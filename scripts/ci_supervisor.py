#!/usr/bin/env python3
"""Start a throwaway Firecracker VM for each queued CI job on Colin's private repos.

Polls GitHub for queued jobs asking for the `homelab-fc` label, on every private
repository of Colin's that its token can see: a repository signs up by being added to
the token's repository access in GitHub, nothing on the host. For each job it asks
GitHub for a just-in-time runner registration (good for exactly one job) and boots a
VM with run-vm.sh, which receives that registration through Firecracker's metadata
service. The GitHub token stays on
the host; a VM only ever sees its one-job registration. A repository that is not
private, or not Colin's, is never served. See docs/runbooks/ci-runner.md.

Installed root-owned by ci-runner/install-host.sh and run by homelab-ci-supervisor.service.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LABEL = "homelab-fc"
RUNNER_LABELS = ("self-hosted", "linux", "x64", LABEL)
# One extra label, `fc-cache-<owner>--<name>`, asks for another served repo's cache disk
# (a job working on that repo's code). GitHub owner names cannot hold `--`.
CACHE_LABEL_PREFIX = "fc-cache-"
# Only Colin's own repositories are served, whatever else the token can see.
OWNER = "ColinCee"
# How often to ask GitHub which repositories the token can see.
REPOS_EVERY = 60
# A repo that has never had a VM here is asked about queued jobs this often, not every
# poll: with every private repo served, polling all of them every 2 s spent GitHub's
# hourly request budget in about 40 minutes.
QUIET_POLL_SECONDS = 60
TOKEN_FILE = Path("/etc/homelab-ci/github-token")
RUN_VM = Path("/usr/local/lib/homelab-ci/run-vm.sh")
STATE_DIR = Path("/run/homelab-ci")
CGROUP_ROOT = Path("/sys/fs/cgroup/firecracker")
API = "https://api.github.com"
# Three VMs at most: a job alone gets 4 GiB, the others 2 GiB each, 8 GiB in all,
# which leaves the home's own services room.
SLOTS = 3
# Unchanged answers are free conditional requests, so polling often costs nothing.
POLL_SECONDS = 2
# A VM that has not picked up a job by now counts as idle demand again.
BOOT_GRACE_SECONDS = 120
# Time for a VM to boot and connect. After it, a VM with no job while nothing is
# queued for its repo (the job was cancelled, or another runner took it) is stopped.
IDLE_AFTER_SECONDS = 30
VM_TIMEOUT_SECONDS = 3600
# One VM is kept booted and registered for the first repo on the list, so a job
# starts in seconds instead of waiting for a VM. It is replaced every 45 minutes
# while idle; its timeout leaves a job that starts just before then time to finish.
STANDBY_RECYCLE_SECONDS = 45 * 60
# How many VMs wait ready at once (each about 450 MB while idle). Read from
# /etc/homelab-ci/standbys if present, so trying 2 needs no code change.
STANDBY_COUNT_FILE = Path("/etc/homelab-ci/standbys")
STANDBY_TIMEOUT_SECONDS = 2 * 3600
# Checked every poll: conditional requests make an unchanged answer free.
PRIVACY_RECHECK_SECONDS = 0
RUNNER_PREFIX = "fc-"
# Each repo's read-only cache disk: its package caches and installs, built from
# an artifact that a green push to its default branch uploads (see ci-runner.md).
CACHE_DIR = Path("/var/lib/homelab-ci/caches")
CACHE_ARTIFACT = "homelab-fc-cache"
CACHE_CHECK_SECONDS = 60
CACHE_MAX_BYTES = 2 * 1024**3
# Unpacked: a zip can expand far beyond its own size, and / is shared with everything.
CACHE_MAX_UNPACKED_BYTES = 6 * 1024**3
RUNNER_UID = 1001  # the runner user inside the VM image


# What a VM is for: its repo, and the extra cache label it registers with ("" for none).
# A VM covers only jobs with the same pair.
type Want = tuple[str, str]


@dataclass(frozen=True)
class Job:
    repo: str
    id: int
    name: str
    labels: tuple[str, ...]

    @property
    def want(self) -> Want:
        return (self.repo, cache_label(self.labels))


@dataclass
class Launch:
    slot: int
    repo: str
    runner_id: int
    runner_name: str
    started: float
    process: subprocess.Popen[bytes]
    metadata: Path
    # Booted ahead of any job; becomes an ordinary job VM once GitHub gives it one.
    standby: bool = False
    # A standby that just took a job still covers it until GitHub's job list catches up.
    covers_until: float = 0.0
    label: str = ""

    @property
    def want(self) -> Want:
        return (self.repo, self.label)


@dataclass
class Privacy:
    private: bool
    checked: float
    default_branch: str = ""


@dataclass
class State:
    launches: dict[int, Launch] = field(default_factory=dict)
    privacy: dict[str, Privacy] = field(default_factory=dict)
    cache_checked: dict[str, float] = field(default_factory=dict)
    cache_busy: threading.Lock = field(default_factory=threading.Lock)
    # The repository of the last job a VM started for: the standby goes to it.
    last_repo: str = ""
    # Repos that have had a VM here (or have a cache disk): polled every poll, the rest
    # every QUIET_POLL_SECONDS, when each was last polled.
    used: set[str] = field(default_factory=set)
    polled: dict[str, float] = field(default_factory=dict)


def log(message: str) -> None:
    print(message, flush=True)


def cache_label_repo(label: str) -> str | None:
    """owner/name (lowercase) named by an `fc-cache-<owner>--<name>` label, else None."""
    label = label.lower()
    owner, sep, name = label.removeprefix(CACHE_LABEL_PREFIX).partition("--")
    if not label.startswith(CACHE_LABEL_PREFIX) or not (sep and owner and name):
        return None
    return f"{owner}/{name}"


def cache_label(labels: Iterable[str]) -> str:
    """The job's cache label, lowercase, or "" if it asks for none."""
    return next((label.lower() for label in labels if cache_label_repo(label)), "")


def wants_us(labels: Iterable[str]) -> bool:
    """A job is ours if it asks for our label and nothing our VMs do not have,
    apart from at most one cache label."""
    asked = {label.lower() for label in labels}
    extra = asked - set(RUNNER_LABELS)
    return LABEL in asked and len(extra) <= 1 and all(map(cache_label_repo, extra))


def queued_jobs(
    repo: str, runs: Iterable[Mapping[str, Any]], get_jobs: Callable[[int], Any]
) -> list[Job]:
    jobs: list[Job] = []
    for run in runs:
        for job in get_jobs(int(run["id"])):
            labels = tuple(str(label) for label in job.get("labels", []))
            if job.get("status") == "queued" and wants_us(labels):
                jobs.append(Job(repo, int(job["id"]), str(job.get("name", "")), labels))
    return jobs


def plan(queued: Mapping[Want, int], booting: Mapping[Want, int], free_slots: int) -> list[Want]:
    """Which VMs start now: one per queued job not already covered by a booting VM."""
    starts: list[Want] = []
    for want in sorted(queued):
        need = max(0, queued[want] - booting.get(want, 0))
        for _ in range(need):
            if len(starts) == free_slots:
                return starts
            starts.append(want)
    return starts


def is_private(repo_data: Mapping[str, Any], repo: str) -> bool:
    return (
        repo_data.get("private") is True
        and str(repo_data.get("full_name", "")).lower() == repo.lower()
    )


class RateLimited(Exception):
    def __init__(self, until: float) -> None:
        super().__init__(f"rate limited until {time.strftime('%H:%M:%S', time.localtime(until))}")
        self.until = until


class GitHub:
    def __init__(self, token: str) -> None:
        self._token = token
        # Conditional GETs: a 304 reply does not count against the rate limit.
        self._cache: dict[str, tuple[str, Any]] = {}

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"{API}{path}", data=data, method=method)
        # Not carried to another host if GitHub ever redirects.
        req.add_unredirected_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        cached = self._cache.get(path) if method == "GET" else None
        if cached:
            req.add_header("If-None-Match", cached[0])
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                raw = response.read()
                etag = response.headers.get("ETag")
        except urllib.error.HTTPError as e:
            if e.code == 304 and cached:
                return cached[1]
            if e.code in (403, 429) and (
                e.headers.get("Retry-After") or e.headers.get("X-RateLimit-Remaining") == "0"
            ):
                until = rate_limit_until(
                    e.headers.get("Retry-After"), e.headers.get("X-RateLimit-Reset"), time.time()
                )
                raise RateLimited(until) from None
            raise
        result = json.loads(raw) if raw else None
        if method == "GET" and etag:
            self._cache[path] = (etag, result)
        return result

    def download(self, path: str, dest: Path, limit: int) -> None:
        """Stream a binary answer to a file; GitHub redirects to signed storage, which
        does not get the token (unredirected header)."""
        req = urllib.request.Request(f"{API}{path}")
        req.add_unredirected_header("Authorization", f"Bearer {self._token}")
        with urllib.request.urlopen(req, timeout=120) as response, dest.open("wb") as f:
            got = 0
            while chunk := response.read(1 << 20):
                got += len(chunk)
                if got > limit:
                    raise ValueError(f"download over {limit} bytes")
                f.write(chunk)

    def clone(self) -> GitHub:
        return GitHub(self._token)

    def forget(self, drop: Callable[[str], bool]) -> None:
        for path in [path for path in self._cache if drop(path)]:
            del self._cache[path]


def rate_limit_until(retry_after: str | None, reset: str | None, now: float) -> float:
    """When to try again: Retry-After seconds, else the reset time, else a minute."""
    if retry_after:
        return now + int(retry_after)
    return float(reset) if reset else now + 60


def check_private(gh: GitHub, state: State, repo: str, now: float, *, fresh: bool = False) -> bool:
    known = state.privacy.get(repo)
    # A repo with VMs here, or about to get its first, is re-checked; the rest are known
    # private from the minutely repo listing (served_repos), one request for all of them,
    # and asked directly if that listing is out of date.
    if (
        known is None
        or fresh
        or now - known.checked > 2 * REPOS_EVERY
        or (repo in state.used and now - known.checked > PRIVACY_RECHECK_SECONDS)
    ):
        data = gh.request("GET", f"/repos/{repo}")
        private = is_private(data, repo)
        if not private:
            log(f"refusing {repo}: not a private repository")
        state.privacy[repo] = Privacy(private, now, str(data.get("default_branch", "")))
    return state.privacy[repo].private


def find_queued(gh: GitHub, repo: str) -> list[Job]:
    runs: list[Mapping[str, Any]] = []
    # A multi-job run is in_progress while its later jobs wait for a runner. A run that
    # waited on a concurrency group can stay pending after its job is queued.
    for status in ("queued", "pending", "in_progress"):
        page = gh.request("GET", f"/repos/{repo}/actions/runs?status={status}&per_page=50")
        runs.extend(page.get("workflow_runs", []))

    def jobs_path(run_id: int) -> str:
        return f"/repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"

    def get_jobs(run_id: int) -> Any:
        return gh.request("GET", jobs_path(run_id)).get("jobs", [])

    # Forget finished runs, so the cache only holds what is still polled.
    live = {jobs_path(int(run["id"])) for run in runs}
    gh.forget(lambda path: path.startswith(f"/repos/{repo}/actions/runs/") and path not in live)
    return queued_jobs(repo, runs, get_jobs)


def delete_runner(gh: GitHub, repo: str, runner_id: int, name: str) -> bool:
    """True if the registration is gone (removed now, or it removed itself)."""
    gh.forget(lambda path: path == f"/repos/{repo}/actions/runners/{runner_id}")
    try:
        gh.request("DELETE", f"/repos/{repo}/actions/runners/{runner_id}")
    except urllib.error.HTTPError as e:
        if e.code == 404:  # the runner removed itself after its job
            return True
        log(f"could not remove runner {name} from {repo}: HTTP {e.code}")
        return False
    return True


def launch(
    gh: GitHub,
    state: State,
    repo: str,
    slot: int,
    now: float,
    *,
    cache: Path | None,
    label: str = "",
    standby: bool = False,
) -> None:
    name = f"{RUNNER_PREFIX}{slot}-{int(now)}"
    # The cache label is registered too, so a job asking for it runs only on such a VM.
    labels = [*RUNNER_LABELS, label] if label else list(RUNNER_LABELS)
    registration = gh.request(
        "POST",
        f"/repos/{repo}/actions/runners/generate-jitconfig",
        {"name": name, "runner_group_id": 1, "labels": labels, "work_folder": "_work"},
    )
    runner_id = int(registration["runner"]["id"])
    metadata = STATE_DIR / f"metadata-slot{slot}.json"
    # A standby outlives the jobs around it, so it is sized for running alone.
    vcpus, mem_mib = vm_size(0 if standby else len(state.launches))
    try:
        fd = os.open(metadata, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"task": "runner", "jitconfig": registration["encoded_jit_config"]}, f)
        process = subprocess.Popen(
            [
                str(RUN_VM),
                str(slot),
                str(metadata),
                str(STANDBY_TIMEOUT_SECONDS if standby else VM_TIMEOUT_SECONDS),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                **os.environ,
                "VCPUS": str(vcpus),
                "MEM_MIB": str(mem_mib),
                "CACHE_IMAGE": str(cache) if cache else "",
            },
        )
    except BaseException:
        metadata.unlink(missing_ok=True)
        delete_runner(gh, repo, runner_id, name)
        raise
    state.launches[slot] = Launch(
        slot, repo, runner_id, name, now, process, metadata, standby, label=label
    )
    state.used.add(repo)
    if not standby:
        state.last_repo = repo
    kind = "standby" if standby else "VM"
    disk = cache.name if cache else "no cache disk"
    log(f"slot {slot}: started {kind} {name} for {repo}, {disk} ({vcpus} vCPUs, {mem_mib} MiB)")


def vm_size(running: int) -> tuple[int, int]:
    """A job alone gets the whole machine's cores; with others running, each gets half."""
    return (4, 4096) if running == 0 else (2, 2048)


def may_be_idle(age: float, queued_for_repo: int) -> bool:
    """Worth asking GitHub whether the VM has a job: it has had time to connect,
    and no queued job in its repo could still be on its way to it."""
    return age >= IDLE_AFTER_SECONDS and queued_for_repo == 0


def is_idle(gh: GitHub, run: Launch) -> bool:
    """GitHub says the VM's runner is connected and has no job."""
    try:
        runner = gh.request("GET", f"/repos/{run.repo}/actions/runners/{run.runner_id}")
    except urllib.error.HTTPError as e:
        if e.code == 404:  # finished and removed itself; the VM is ending
            return False
        raise
    return runner.get("busy") is False


def stop(run: Launch, why: str) -> None:
    log(f"slot {run.slot}: {run.runner_name} {why}, stopping it")
    run.process.terminate()
    # Do not rely on run-vm.sh's trap alone: the VM lives in its own cgroup.
    kill = CGROUP_ROOT / f"ci-slot{run.slot}" / "cgroup.kill"
    if kill.exists():
        kill.write_text("1")


def stop_if_still_idle(gh: GitHub, run: Launch, why: str) -> None:
    """Asked twice, so a job assigned between the two answers is not killed."""
    if is_idle(gh, run):
        time.sleep(2)
        if is_idle(gh, run):
            stop(run, why)


def stop_idle(gh: GitHub, run: Launch, now: float, queued_for_repo: int) -> None:
    """A VM whose job was cancelled or taken by another runner would wait out its
    timeout; stop it as soon as it is clearly not going to get one."""
    if may_be_idle(now - run.started, queued_for_repo):
        stop_if_still_idle(gh, run, "has no job")


def check_standby(gh: GitHub, run: Launch, now: float, private: bool) -> None:
    """A standby that got a job becomes an ordinary VM; an old idle one is replaced."""
    if not private:
        stop(run, "serves a repo that is no longer private")
    elif not is_idle(gh, run):
        if now - run.started >= IDLE_AFTER_SECONDS:  # connected, so not idle means busy
            run.standby = False
            run.covers_until = now + 10
            log(f"slot {run.slot}: standby {run.runner_name} took a job")
    elif now - run.started >= STANDBY_RECYCLE_SECONDS:
        stop_if_still_idle(gh, run, "is an old standby")


def reap(
    gh: GitHub,
    state: State,
    now: float,
    queued: Mapping[Want, int] | None = None,
    private: Iterable[str] = (),
) -> None:
    """Clear finished VMs. With this poll's queue, also stop VMs left without a job."""
    private = set(private)
    for slot, run in list(state.launches.items()):
        code = run.process.poll()
        if code is None:
            if queued is None:
                continue
            if run.standby:
                check_standby(gh, run, now, run.repo in private)
            else:
                stop_idle(gh, run, now, queued.get(run.want, 0))
            continue
        run.metadata.unlink(missing_ok=True)
        delete_runner(gh, run.repo, run.runner_id, run.runner_name)
        log(f"slot {slot}: {run.runner_name} finished (exit {code})")
        del state.launches[slot]


# Each repo GitHub would not show on the last try, with why; logged only when it changes.
SKIPPED: dict[str, str] = {}


def skipped(repo: str, error: Exception | None) -> None:
    """Notes one repo failing (or working again), so a lasting failure logs once, not every poll."""
    why = f"{type(error).__name__}: {error}" if error else None
    if why and SKIPPED.get(repo) != why:
        log(f"skipping {repo} until GitHub shows it: {why}")
        SKIPPED[repo] = why
    elif not why and SKIPPED.pop(repo, None):
        log(f"{repo} is back")


def remove_stale_runners(gh: GitHub, repos: Iterable[str]) -> list[str]:
    """Registrations left by a previous supervisor; their VMs died with it. Returns the
    repos GitHub would not show (say, the token cannot see them), to try again later."""
    failed: list[str] = []
    for repo in repos:
        try:
            page = gh.request("GET", f"/repos/{repo}/actions/runners?per_page=100")
        except RateLimited:
            raise
        except Exception as e:
            skipped(f"{repo} runners", e)
            failed.append(repo)
            continue
        for runner in page.get("runners", []):
            name = str(runner.get("name", ""))
            if name.startswith(RUNNER_PREFIX) and delete_runner(gh, repo, int(runner["id"]), name):
                log(f"removed stale runner {name} from {repo}")
    return failed


def cache_image(repo: str) -> Path:
    return CACHE_DIR / f"{repo.replace('/', '__')}.ext4"


def cache_for(repo: str, label: str, private: Iterable[str]) -> Path | None:
    """A VM's cache disk: its own repo's, or the one its label names if that repo is
    served, private (`private` is this poll's served private repos) and has a disk."""
    if not label:
        return cache_image(repo)
    served = {r.lower(): r for r in private}
    named = served.get(cache_label_repo(label) or "")
    image = cache_image(named) if named else None
    return image if image and image.exists() else None


def pick_cache_artifact(
    artifacts: Iterable[Mapping[str, Any]], branch: str
) -> Mapping[str, Any] | None:
    """The newest unexpired cache artifact made on the default branch."""
    made_there = [
        a
        for a in artifacts
        if a.get("name") == CACHE_ARTIFACT
        and a.get("expired") is False
        and (a.get("workflow_run") or {}).get("head_branch") == branch
    ]
    return max(made_there, key=lambda a: str(a.get("created_at", "")), default=None)


def safe_member(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
    """Plain files, folders and links only, nothing outside the folder (tar filter),
    and no device or sparse files: the host only unpacks this into a disk image."""
    if (
        not (member.isfile() or member.isdir() or member.issym() or member.islnk())
        or member.issparse()
    ):
        raise tarfile.FilterError(f"not a plain file, folder or link: {member.name}")
    return tarfile.tar_filter(member, dest)


def size_capped(limit: int) -> Callable[[tarfile.TarInfo, str], tarfile.TarInfo | None]:
    """safe_member, refusing the archive once its files add up to more than limit."""
    total = 0

    def check(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
        nonlocal total
        total += member.size
        if total > limit:
            raise tarfile.FilterError(f"unpacks to more than {limit} bytes")
        return safe_member(member, dest)

    return check


def build_cache_image(zip_path: Path, image: Path) -> None:
    """Artifact zip -> cache.tar -> a folder -> an ext4 image, owned by the VM's runner user.
    Runs as root on the host and never executes anything from the artifact."""
    with tempfile.TemporaryDirectory(dir=CACHE_DIR) as tmp:
        root = Path(tmp) / "root"
        root.mkdir()
        with (
            zipfile.ZipFile(zip_path) as z,
            z.open("cache.tar") as raw,
            tarfile.open(fileobj=raw, mode="r|") as tar,
        ):
            tar.extractall(root, filter=size_capped(CACHE_MAX_UNPACKED_BYTES))
        # Owned by the VM's runner user, so the job can write through its throwaway layer.
        # The folder is in a root-only temporary directory, so no host user reaches it.
        subprocess.run(["chown", "-R", f"{RUNNER_UID}:{RUNNER_UID}", str(root)], check=True)
        size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file() and not f.is_symlink())
        staged = Path(tmp) / "cache.ext4"
        with staged.open("wb") as f:
            f.truncate(int(size * 1.3) + 256 * 1024**2)
        subprocess.run(
            [
                "mkfs.ext4",
                "-q",
                "-F",
                "-L",
                "cache",
                "-E",
                f"root_owner={RUNNER_UID}:{RUNNER_UID}",
                "-d",
                str(root),
                str(staged),
            ],
            check=True,
        )
        subprocess.run(["e2fsck", "-fp", str(staged)], check=False, capture_output=True)
        staged.chmod(0o644)
        staged.replace(image)


def refresh_cache(gh: GitHub, repo: str, branch: str) -> None:
    """Rebuild the repo's cache disk when a newer artifact from a green push to its
    default branch exists. A branch cannot replace it: only those runs count."""
    image = cache_image(repo)
    seen = image.with_suffix(".json")
    page = gh.request("GET", f"/repos/{repo}/actions/artifacts?name={CACHE_ARTIFACT}&per_page=20")
    artifact = pick_cache_artifact(page.get("artifacts", []), branch)
    if artifact is None:
        return
    known = json.loads(seen.read_text()) if seen.exists() else {}
    if known.get("artifact") == artifact["id"]:
        return
    run = gh.request("GET", f"/repos/{repo}/actions/runs/{artifact['workflow_run']['id']}")
    if run.get("status") != "completed":
        return  # uploaded before its run finished: judge it on a later check
    if not (
        run.get("event") == "push"
        and run.get("conclusion") == "success"
        and run.get("head_branch") == branch
    ):
        seen.write_text(json.dumps({**known, "artifact": artifact["id"], "refused": True}))
        log(f"cache for {repo}: artifact {artifact['id']} is not from a green push; ignored")
        return
    if int(artifact.get("size_in_bytes", 0)) > CACHE_MAX_BYTES:
        log(f"cache for {repo}: artifact {artifact['id']} is too big; ignored")
        return
    started = time.time()
    with tempfile.TemporaryDirectory(dir=CACHE_DIR) as tmp:
        zip_path = Path(tmp) / "artifact.zip"
        gh.download(
            f"/repos/{repo}/actions/artifacts/{artifact['id']}/zip", zip_path, CACHE_MAX_BYTES
        )
        build_cache_image(zip_path, image)
    seen.write_text(
        json.dumps({"artifact": artifact["id"], "run": run["id"], "sha": run.get("head_sha", "")})
    )
    log(f"cache for {repo}: built from run {run['id']} in {time.time() - started:.0f}s")


def maybe_refresh_caches(gh: GitHub, state: State, private: list[str], now: float) -> None:
    """At most one rebuild at a time, in the background, so polling never waits on it."""
    due = [r for r in private if now - state.cache_checked.get(r, 0) >= CACHE_CHECK_SECONDS]
    if not due or not state.cache_busy.acquire(blocking=False):
        return
    for repo in due:
        state.cache_checked[repo] = now

    # Its own client: the request cache is not shared between threads.
    own = gh.clone()
    branches = {repo: state.privacy[repo].default_branch for repo in due}

    def work() -> None:
        try:
            for repo in due:
                refresh_cache(own, repo, branches[repo])
        except Exception as e:
            log(f"cache refresh failed: {type(e).__name__}: {e}")
        finally:
            state.cache_busy.release()

    threading.Thread(target=work, daemon=True).start()


def tick(gh: GitHub, state: State, repos: list[str], now: float) -> None:
    queued: Counter[Want] = Counter()
    private: list[str] = []
    for repo in repos:
        # A repo that is no longer private has nothing queued as far as we are
        # concerned, so its idle VMs are stopped below and no new ones start. One
        # repo GitHub will not show is skipped this poll; it never stops the others
        # (3 Nov: a repo the token could not see stopped all CI).
        try:
            if check_private(gh, state, repo, now):
                jobs = find_queued(gh, repo)
                # A repo about to get its first VM is asked about its privacy directly.
                if (
                    jobs
                    and repo not in state.used
                    and not (
                        check_private(gh, state, repo, now, fresh=True) and only_colin(gh, repo)
                    )
                ):
                    continue
                private.append(repo)
                queued.update(job.want for job in jobs)
            skipped(repo, None)
        except RateLimited:
            raise
        except Exception as e:
            skipped(repo, e)
    reap(gh, state, now, queued, private)
    # Cache disks only for repos that run here; a quiet repo gets checked after its first job.
    maybe_refresh_caches(
        gh, state, [r for r in private if r in state.used or cache_image(r).exists()], now
    )
    # A VM still on its way to a job, or a standby waiting for one, covers a queued job
    # with the same repo and cache label (or none).
    booting: Counter[Want] = Counter(
        run.want
        for run in state.launches.values()
        if run.standby or now < run.covers_until or now - run.started < BOOT_GRACE_SECONDS
    )
    free = [slot for slot in range(SLOTS) if slot not in state.launches]
    starts = plan(queued, booting, len(free))
    for slot, (repo, label) in zip(free, starts, strict=False):
        launch(gh, state, repo, slot, now, label=label, cache=cache_for(repo, label, private))
    free = free[len(starts) :]
    # A job still waiting for a slot gets the standby's, if the standby cannot take it.
    waiting = queued - booting
    waiting.subtract(starts)
    for run in list(state.launches.values()):
        if run.standby and any(n > 0 and want != run.want for want, n in waiting.items()):
            stop_if_still_idle(gh, run, "is in the way of a queued job")
    if wants_standby(
        repos, private, [r.standby for r in state.launches.values()], len(free), standby_count()
    ):
        launch(gh, state, repos[0], free[0], now, cache=cache_image(repos[0]), standby=True)


def wants_standby(
    repos: list[str], private: list[str], standbys: list[bool], free: int, wanted: int = 1
) -> bool:
    """Up to `wanted` standby VMs for the first repo (the last job's), while private and free."""
    return bool(repos) and repos[0] in private and sum(standbys) < wanted and free > 0


def standby_count() -> int:
    try:
        return max(0, int(STANDBY_COUNT_FILE.read_text().strip()))
    except (OSError, ValueError):
        return 1


def served_repos(gh: GitHub) -> dict[str, str]:
    """Colin's private repositories the token can see, the ones that signed up, each
    with its default branch."""
    repos: list[dict] = []
    for n in range(1, 11):
        page = gh.request(
            "GET", f"/user/repos?visibility=private&affiliation=owner&per_page=100&page={n}"
        )
        repos += page
        if len(page) < 100:
            break
    return {
        str(r["full_name"]): str(r.get("default_branch", ""))
        for r in sorted(repos, key=lambda r: str(r["full_name"]))
        if r.get("private") is True and (r.get("owner") or {}).get("login") == OWNER
    }


def only_colin(gh: GitHub, repo: str) -> bool:
    """Whether nobody but Colin can push to the repo: no other collaborator and no deploy
    key that can write. Asked on every listing and before a repo's first VM (unchanged
    answers are free); any doubt is a no, so only Colin and his own apps start jobs here."""
    try:
        people = gh.request("GET", f"/repos/{repo}/collaborators?affiliation=direct&per_page=100")
        keys = gh.request("GET", f"/repos/{repo}/keys?per_page=100")
    except RateLimited:
        raise
    except Exception as e:
        skipped(f"{repo} collaborators", e)
        return False
    skipped(f"{repo} collaborators", None)
    return all((p.get("login") or "") == OWNER for p in people) and all(
        k.get("read_only") is True for k in keys
    )


def due(repos: list[str], state: State, now: float) -> list[str]:
    """The repos to poll now: every one that has had a VM or has a cache disk, and the
    quiet rest once a minute each."""
    return [
        r
        for r in repos
        if r in state.used
        or cache_image(r).exists()
        or now - state.polled.get(r, 0) >= QUIET_POLL_SECONDS
    ]


def standby_first(repos: list[str], last: str) -> list[str]:
    """The repo of the last job first, so the standby VM waits where work just was."""
    return [last, *(r for r in repos if r != last)] if last in repos else repos


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="one poll, for testing")
    args = parser.parse_args(argv)
    gh = GitHub(TOKEN_FILE.read_text().strip())
    repos: list[str] = []
    listed = 0.0
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    CACHE_DIR.mkdir(mode=0o755, exist_ok=True)
    # Half-built caches from a supervisor that was killed mid-build.
    for leftover in CACHE_DIR.glob("tmp*"):
        subprocess.run(["rm", "-rf", "--one-file-system", str(leftover)], check=False)
    state = State()
    # Repos whose stale registrations are not cleared yet.
    unclean: list[str] = []
    failures = 0
    log(f"{SLOTS} slots; serving private repos the token can see")
    while True:
        wait: float = POLL_SECONDS
        # Any failure is logged and retried; exiting would kill the running jobs.
        try:
            if time.time() - listed >= REPOS_EVERY:
                listed = time.time()
                # A failed listing keeps the last list (none at start), never ending the poll.
                try:
                    listing = served_repos(gh)
                    found = [r for r in listing if only_colin(gh, r)]
                    # Listed private just now: quiet repos need no privacy request of their own.
                    for repo, branch in listing.items():
                        state.privacy[repo] = Privacy(True, listed, branch)
                    skipped("the repo list", None)
                except RateLimited:
                    raise
                except Exception as e:
                    skipped("the repo list", e)
                    found = repos
                if found != repos:
                    log(f"serving {len(found)} repositories: {', '.join(found)}")
                    unclean = [r for r in unclean if r in found] + [
                        r for r in found if r not in repos
                    ]
                    repos = found
            if unclean:
                unclean = remove_stale_runners(gh, unclean)
            now = time.time()
            polling = due(standby_first(repos, state.last_repo), state, now)
            tick(gh, state, polling, now)
            for repo in polling:
                state.polled[repo] = now
            failures = 0
        except RateLimited as e:
            log(str(e))
            wait = max(POLL_SECONDS, e.until - time.time() + 1)
        except Exception as e:
            log(f"poll failed: {type(e).__name__}: {e}")
            failures += 1
            # Without GitHub nothing re-checks privacy, so no VM is left waiting meanwhile.
            if failures == 5:
                for run in [r for r in state.launches.values() if r.standby]:
                    stop(run, "cannot be checked while GitHub fails")
        if args.once:
            while state.launches:
                time.sleep(POLL_SECONDS)
                reap(gh, state, time.time())
            return 0
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
