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
import http.client
import io
import json
import os
import re
import signal
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
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

LABEL = "homelab-fc"
RUNNER_LABELS = ("self-hosted", "linux", "x64", LABEL)
# Light jobs (Auto Kaizen's gate, merge, Watcher and other small jobs) get small VMs in
# slots of their own, after the build slots, so they never hold up a build and a merge
# waiting on CI never takes the slot that CI needs. A light VM registers without LABEL,
# so GitHub never gives it a build job.
LIGHT_LABEL = "homelab-fc-light"
LIGHT_RUNNER_LABELS = ("self-hosted", "linux", "x64", LIGHT_LABEL)
LIGHT_SLOTS_FILE = Path("/etc/homelab-ci/light-slots")
# None until install-host.sh writes the file, as it also makes their users and taps.
DEFAULT_LIGHT_SLOTS = 0
# One core, as they mostly wait; 2 GiB, as the merge job runs Claude Code with no swap.
LIGHT_SIZE = (1, 2048)
# A merge job waits for a pull request's CI, two rounds at most.
LIGHT_TIMEOUT_SECONDS = 3 * 3600
# One extra label, `fc-cache-<owner>--<name>`, asks for another served repo's cache disk
# (a job working on that repo's code). GitHub owner names cannot hold `--`.
CACHE_LABEL_PREFIX = "fc-cache-"
# Only Colin's own repositories are served, whatever else the token can see.
OWNER = "ColinCee"
# How often to ask GitHub which repositories the token can see. The listing and its
# collaborator checks (two requests a repo) run beside the poll, never holding it up.
REPOS_EVERY = 60
# A repo that has never had a VM here is asked about queued jobs this often, not every
# poll: with every private repo served, polling all of them every 2 s spent GitHub's
# hourly request budget in about 40 minutes.
QUIET_POLL_SECONDS = 60
# At most this many quiet repos per poll, the longest unasked first. All of them in one
# poll (three requests each, about 40 repos), with the listing beside them, made that
# poll take most of a minute while a queued job waited (4 Nov: 44 to 106 s to its VM).
QUIET_PER_POLL = 3
# A poll slower than this is logged with what it spent its time on.
SLOW_POLL_SECONDS = 5
# Repos asked about queued jobs at once: more than a poll ever asks (the used ones and
# QUIET_PER_POLL more), so a poll costs about one repo's asking.
ASK_THREADS = 16
# The repo and cache label of the last build job, one per line, kept across restarts so
# the standby goes back to them. Older versions wrote the repo alone.
LAST_REPO_FILE = Path("/var/lib/homelab-ci/last-repo")
TOKEN_FILE = Path("/etc/homelab-ci/github-token")
RUN_VM = Path("/usr/local/lib/homelab-ci/run-vm.sh")
STATE_DIR = Path("/run/homelab-ci")
# Each running VM is a systemd unit of its own (homelab-ci-vm-<runner name>), so it outlives a
# restart of this supervisor; its record here, written before it starts, lets a restarted
# supervisor take it back (5 Nov: restarting killed every VM, and 20 jobs were cut off).
VMS_DIR = STATE_DIR / "vms"
UNIT_PREFIX = "homelab-ci-vm-"
CGROUP_ROOT = Path("/sys/fs/cgroup/firecracker")
API_HOST = "api.github.com"
API = f"https://{API_HOST}"
# How many VMs run at once, read from /etc/homelab-ci/slots at start-up. install-host.sh
# writes it and makes that many slot users and taps, so the three always match. A job
# alone gets 4 GiB and each one started beside it 2 GiB (see vm_size), so N slots use
# up to 2 + 2N GiB on paper. The default 5 (12 GiB on paper) fits the 16 GiB host because
# jobs mostly wait on the network and touch far less than their size, and the memory
# check below holds a VM back rather than eat into the services' reserve.
# Auto Kaizen caps its running tasks at this number (its build.yml and watch.yml).
SLOTS_FILE = Path("/etc/homelab-ci/slots")
DEFAULT_SLOTS = 5
# Unchanged answers are free conditional requests, so polling often costs nothing.
POLL_SECONDS = 2
# A VM covers a queued job of its repo and label only while it is starting: until its runner
# takes a job, and for at most this long. VMs start in seconds since the disk image is no
# longer copied (homelab#759); 600 s, set while copies took minutes, made a busy VM hold back
# the next job of its kind with slots free (5 Nov: a task's Reviewer behind its Builder).
BOOT_GRACE_SECONDS = 120
# Time for a VM to boot and connect. After it, a VM with no job while nothing is
# queued for its repo (the job was cancelled, or another runner took it) is stopped.
IDLE_AFTER_SECONDS = 30
VM_TIMEOUT_SECONDS = 3600
# One VM is kept booted and registered for the last job's repo, so a job
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
# What the CI runner is doing, for the Control room: rewritten after every poll, readable
# by anyone on the host. Nothing secret goes in it (see status()).
STATUS_FILE = Path("/var/lib/homelab-ci/status/machines.json")
# Each running Builder's and Reviewer's latest live line, read from its VM's console, for
# the Control room (auto-kaizen's docs/live-view.md). Only that repo's VMs count, and only
# lines in its exact format; the rest of the console is read and dropped, never logged.
LIVE_FILE = STATUS_FILE.with_name("live.json")
LIVE_REPO = "ColinCee/auto-kaizen"
LIVE_LINE = re.compile(rb"KAIZEN-LIVE 1 ([0-9]{1,9}) (builder|reviewer) ([0-9]{10}) (.+)")
LIVE_TEXT_MAX = 120
# Each auto-kaizen VM's agent sessions, one event per console line, for the Control room's
# live chat page (auto-kaizen's docs/live-session.md). Filed under the VM's own runner name,
# never anything the line says, so one VM cannot write into another's session. Root only:
# sessions can carry Colin's private notes.
SESSIONS_DIR = STATUS_FILE.with_name("sessions")
SESSION_LINE = re.compile(rb"KAIZEN-SESSION 1 (\{[ -~]*\})")
SESSION_MAX_BYTES = 30 * 1024**2
SESSION_KEEP_SECONDS = 30 * 24 * 3600
CONSOLE_LINE_MAX = 4096
# A VM starts only if the host's available memory, less the VM's size, still leaves this
# much for the home's own services (the host's 16 GiB are shared with them). A booted VM
# touches far less than its size, so the check uses what is really free, not the sum of
# the sizes. /etc/homelab-ci/memory-reserve-mib changes it with no code change.
MEMINFO = Path("/proc/meminfo")
MEMORY_RESERVE_FILE = Path("/etc/homelab-ci/memory-reserve-mib")
DEFAULT_MEMORY_RESERVE_MIB = 3 * 1024
# No VM starts while tasks waited on the disk for more than DISK_WAIT_LIMIT percent of
# the last minute (`some avg60` in /proc/pressure/io), and over half that, one a minute,
# since a new VM's load takes about a minute to show; running VMs carry on. VM disks and
# swap share the host's one SSD with Grafana's database: on 5 Nov 9 VMs kept it at 70-95%,
# Grafana's checks failed on a locked database and posted false alerts. 50 is a judgement
# from hourly samples: quiet hours stayed under it, and Grafana already failed at some
# samples near it, hence the pacing below it.
IO_PRESSURE = Path("/proc/pressure/io")
DISK_WAIT_LIMIT = 50.0
DISK_PACE_SECONDS = 60.0


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
        if LIGHT_LABEL in {label.lower() for label in self.labels}:
            return (self.repo, LIGHT_LABEL)
        return (self.repo, cache_label(self.labels))


@dataclass
class Launch:
    slot: int
    repo: str
    runner_id: int
    runner_name: str
    started: float
    metadata: Path
    # Booted ahead of any job; becomes an ordinary job VM once GitHub gives it one.
    standby: bool = False
    # A standby that just took a job still covers it until GitHub's job list catches up.
    covers_until: float = 0.0
    label: str = ""
    # GitHub showed its runner with a job: it no longer covers a queued one.
    busy: bool = False

    @property
    def unit(self) -> str:
        return f"{UNIT_PREFIX}{self.runner_name}"

    @property
    def record(self) -> Path:
        return VMS_DIR / f"{self.runner_name}.json"

    @property
    def live_file(self) -> Path:
        """Its latest live line, written by its console reader (`vm` mode)."""
        return VMS_DIR / f"{self.runner_name}.live"

    @property
    def want(self) -> Want:
        return (self.repo, self.label)

    @property
    def light(self) -> bool:
        return self.label == LIGHT_LABEL


@dataclass
class Privacy:
    private: bool
    checked: float
    default_branch: str = ""


@dataclass
class State:
    slots: int = DEFAULT_SLOTS
    # Light slots, numbered after the build slots.
    light_slots: int = 0
    launches: dict[int, Launch] = field(default_factory=dict)
    privacy: dict[str, Privacy] = field(default_factory=dict)
    cache_checked: dict[str, float] = field(default_factory=dict)
    cache_busy: threading.Lock = field(default_factory=threading.Lock)
    # The threads that ask the polled repos for queued jobs at once, kept so each keeps
    # its open connection to GitHub.
    askers: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(ASK_THREADS, thread_name_prefix="ask")
    )
    # The repository and cache label of the last build job a VM started for: the standby
    # boots for the same, so the next such job (a task's Builder, then its Reviewer) takes it.
    last_repo: str = ""
    last_label: str = ""
    # Repos that have had a job here (or have a cache disk): polled every poll, the rest
    # every QUIET_POLL_SECONDS, when each was last polled.
    used: set[str] = field(default_factory=set)
    polled: dict[str, float] = field(default_factory=dict)
    # Queued jobs not started for lack of memory or a busy disk (job id -> repo), each
    # logged once.
    held: dict[int, str] = field(default_factory=dict)
    # While the disk is half busy, no VM starts before this time (see DISK_PACE_SECONDS).
    disk_paced_until: float = 0.0


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
    if LIGHT_LABEL in asked:
        return asked <= set(LIGHT_RUNNER_LABELS)
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


def plan(oldest_first: Iterable[Want], booting: Mapping[Want, int], free_slots: int) -> list[Want]:
    """Which VMs start now: one per queued job (each given by its want, oldest job first)
    not already covered by a booting VM. Oldest first, so a job wanting a rarer cache is
    not starved by a stream of newer ones (5 Nov: one sorted last waited 1h45)."""
    cover = Counter(booting)
    starts: list[Want] = []
    for want in oldest_first:
        if cover[want] > 0:
            cover[want] -= 1
        elif len(starts) < free_slots:
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
        # Conditional GETs: a 304 reply does not count against the rate limit. The threads
        # asking repos at once share it, so they take the lock to touch it or the counts.
        self._cache: dict[str, tuple[str, Any]] = {}
        self._lock = threading.Lock()
        # One connection per thread, kept open across requests, so a request does not wait
        # for a new connection and its TLS handshake (4 Nov: about 0.45 s a request).
        self._local = threading.local()
        # Requests made and seconds spent on them, for the slow-poll log.
        self.requests = 0
        self.seconds = 0.0

    def _send(
        self, method: str, path: str, data: bytes | None, headers: Mapping[str, str]
    ) -> tuple[http.client.HTTPResponse, bytes]:
        """One request on this thread's open connection. One GitHub closed while it sat
        idle fails at once and is tried again on a new one; a POST (a VM's registration)
        always gets a new one, so it is never sent twice."""
        conn: http.client.HTTPSConnection | None = getattr(self._local, "conn", None)
        if method == "POST" and conn is not None:
            conn.close()
            conn = None
        while True:
            reused = conn is not None
            if conn is None:
                conn = self._local.conn = http.client.HTTPSConnection(API_HOST, timeout=20)
            try:
                conn.request(method, path, body=data, headers=dict(headers))
                response = conn.getresponse()
                return response, response.read()
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
                conn.close()
                if not reused:
                    raise
                conn = None
            except BaseException:
                conn.close()
                raise

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        # Sent only to api.github.com: redirects are not followed.
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "homelab-ci-supervisor",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        with self._lock:
            cached = self._cache.get(path) if method == "GET" else None
            self.requests += 1
        if cached:
            headers["If-None-Match"] = cached[0]
        started = time.monotonic()
        try:
            response, raw = self._send(method, path, data, headers)
        finally:
            with self._lock:
                self.seconds += time.monotonic() - started
        code, reply = response.status, response.headers
        if code == 304 and cached:
            return cached[1]
        if code in (403, 429) and (
            reply.get("Retry-After") or reply.get("X-RateLimit-Remaining") == "0"
        ):
            until = rate_limit_until(
                reply.get("Retry-After"), reply.get("X-RateLimit-Reset"), time.time()
            )
            raise RateLimited(until)
        if not 200 <= code < 300:
            url = f"{API}{path}"
            raise urllib.error.HTTPError(url, code, response.reason, reply, io.BytesIO(raw))
        result = json.loads(raw) if raw else None
        if method == "GET" and (etag := reply.get("ETag")):
            with self._lock:
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
        with self._lock:
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
    light = label == LIGHT_LABEL
    # The cache label is registered too, so a job asking for it runs only on such a VM.
    if light:
        labels = list(LIGHT_RUNNER_LABELS)
    else:
        labels = [*RUNNER_LABELS, label] if label else list(RUNNER_LABELS)
    registration = gh.request(
        "POST",
        f"/repos/{repo}/actions/runners/generate-jitconfig",
        {"name": name, "runner_group_id": 1, "labels": labels, "work_folder": "_work"},
    )
    runner_id = int(registration["runner"]["id"])
    metadata = STATE_DIR / f"metadata-slot{slot}.json"
    vcpus, mem_mib = LIGHT_SIZE if light else next_size(state, standby)
    timeout = (
        LIGHT_TIMEOUT_SECONDS
        if light
        else STANDBY_TIMEOUT_SECONDS
        if standby
        else VM_TIMEOUT_SECONDS
    )
    run = Launch(slot, repo, runner_id, name, now, metadata, standby, label=label)
    try:
        fd = os.open(metadata, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"task": "runner", "jitconfig": registration["encoded_jit_config"]}, f)
        # The record first: a supervisor that dies right after starting the VM still finds it.
        write_status(run.record, record(run))
        env = {"VCPUS": str(vcpus), "MEM_MIB": str(mem_mib), "CACHE_IMAGE": str(cache or "")}
        start_vm(run, timeout, env)
    except BaseException:
        # It may have started anyway (systemd-run timing out, say).
        subprocess.run(["systemctl", "stop", "--no-block", run.unit], check=False, timeout=30)
        forget(run)
        delete_runner(gh, repo, runner_id, name)
        raise
    state.launches[slot] = run
    # Only a job makes a repo polled every time, not a standby booted for it (4 Nov: one
    # booted after a restart kept a repo with no work polled every poll).
    # A light job neither keeps its repo polled every time nor draws the standby to it.
    if not standby and not light:
        state.used.add(repo)
    if not standby and not light and (repo, label) != (state.last_repo, state.last_label):
        state.last_repo, state.last_label = repo, label
        remember_last_job(repo, label)
    kind = "standby" if standby else "VM"
    disk = cache.name if cache else "no cache disk"
    log(f"slot {slot}: started {kind} {name} for {repo}, {disk} ({vcpus} vCPUs, {mem_mib} MiB)")


def remember_last_job(repo: str, label: str) -> None:
    """Kept for the next start; losing it only sends the first standby elsewhere."""
    try:
        LAST_REPO_FILE.write_text(f"{repo}\n{label}\n")
    except OSError as e:
        log(f"could not keep the last job's repo: {type(e).__name__}: {e}")


def recalled_last_job() -> Want:
    """The last build job's repo and cache label ("" for none, as from an older file)."""
    try:
        lines = LAST_REPO_FILE.read_text().split()
    except OSError:
        return ("", "")
    return (lines[0] if lines else "", lines[1] if len(lines) > 1 else "")


def live_line(raw: bytes, heard: float) -> dict[str, Any] | None:
    """One console line as a live line, or None unless it is exactly in the live format."""
    match = LIVE_LINE.fullmatch(raw.removesuffix(b"\n").removesuffix(b"\r"))
    if not match:
        return None
    try:
        text = match[4].decode()
    except UnicodeDecodeError:
        return None
    if len(text) > LIVE_TEXT_MAX or not text.isprintable():
        return None
    return {
        "task": int(match[1]),
        "who": match[2].decode(),
        "at": int(match[3]),
        "heard": int(heard),
        "text": text,
    }


def record(run: Launch) -> dict[str, Any]:
    """What a restarted supervisor needs to take a running VM back: its launch facts."""
    return {
        "slot": run.slot,
        "repo": run.repo,
        "runner_id": run.runner_id,
        "runner_name": run.runner_name,
        "started": run.started,
        "metadata": str(run.metadata),
        "standby": run.standby,
        "label": run.label,
    }


def start_vm(run: Launch, timeout: int, env: Mapping[str, str]) -> None:
    """Start the VM as a systemd unit of its own, so a restart of this supervisor leaves it
    running. Its console goes through this script's `vm` mode, which keeps the live lines and
    sessions of auto-kaizen's VMs only, so a product's VM cannot put a line on a task. The
    unit stops before the CI network does, at shutdown."""
    vm = [str(run.slot), str(run.metadata), str(timeout)]
    if run.repo.lower() == LIVE_REPO.lower():
        session = SESSIONS_DIR / f"{run.runner_name}.jsonl"
        vm += ["--live", str(run.live_file), "--session", str(session)]
    subprocess.run(
        [
            "systemd-run",
            f"--unit={run.unit}",
            "--collect",
            "--quiet",
            # Stops before the network does at shutdown; Wants, not Requires, as the network
            # unit says: restarting it must not take running VMs with it.
            "--property=After=homelab-ci-net.service",
            "--property=Wants=homelab-ci-net.service",
            "--property=SyslogIdentifier=homelab-ci-vm",
            *(f"--setenv={k}={v}" for k, v in env.items()),
            sys.executable,
            "-I",
            str(Path(__file__).resolve()),
            "vm",
            *vm,
        ],
        check=True,
        timeout=30,
    )


def running_vms() -> set[str] | None:
    """The VM units still running (stopping counts), or None if systemd could not be asked."""
    try:
        out = subprocess.run(
            [
                "systemctl",
                "list-units",
                "--plain",
                "--no-legend",
                "--state=active,activating,deactivating",
                f"{UNIT_PREFIX}*",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return {line.split()[0].removesuffix(".service") for line in out.splitlines() if line.strip()}


def forget(run: Launch) -> None:
    """A VM's files, once it has ended."""
    for path in (run.record, run.live_file, run.metadata):
        path.unlink(missing_ok=True)


def adopt(state: State) -> None:
    """Take back the VMs a previous supervisor started and that still run; delete the records
    of those that ended, and stop a VM unit without a record (nothing could track it)."""
    running = running_vms()
    if running is None:
        raise RuntimeError("cannot list the running VMs")
    VMS_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    for path in VMS_DIR.glob("*.json"):
        try:
            fields = json.loads(path.read_text())
            run = Launch(
                int(fields["slot"]),
                str(fields["repo"]),
                int(fields["runner_id"]),
                str(fields["runner_name"]),
                float(fields["started"]),
                Path(fields["metadata"]),
                standby=bool(fields["standby"]),
                label=str(fields["label"]),
            )
        except (OSError, ValueError, TypeError, KeyError):
            path.unlink(missing_ok=True)
            continue
        if run.unit in running and run.slot not in state.launches:
            state.launches[run.slot] = run
            log(f"slot {run.slot}: took back {run.runner_name} for {run.repo}")
        else:
            forget(run)
    known = {run.unit for run in state.launches.values()}
    for unit in running - known:
        log(f"{unit} has no record, stopping it")
        subprocess.run(["systemctl", "stop", "--no-block", unit], check=False, timeout=30)


def vm_main(argv: list[str]) -> int:
    """`vm SLOT METADATA TIMEOUT [--live FILE --session FILE]`: run-vm.sh's console reader,
    the main process of the VM's unit. It reads the console to the end (a full pipe would
    stall the VM), keeps the latest live line in FILE and the session events, and exits with
    run-vm.sh's code."""
    parser = argparse.ArgumentParser(prog="ci_supervisor.py vm")
    parser.add_argument("slot")
    parser.add_argument("metadata")
    parser.add_argument("timeout")
    parser.add_argument("--live", type=Path)
    parser.add_argument("--session", type=Path)
    args = parser.parse_args(argv)
    # Stopping the unit signals every process in it: run-vm.sh cleans up on its own, and this
    # reader keeps reading until it has (a closed pipe would cut its cleanup short).
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    process = subprocess.Popen(
        [str(RUN_VM), args.slot, args.metadata, args.timeout],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    keep = (lambda line: write_status(args.live, line)) if args.live else None
    read_console(process.stdout, keep, args.session)
    return process.wait()


def session_event(raw: bytes) -> bytes | None:
    """One console line as a session event (a JSON object in printable ASCII), or None."""
    match = SESSION_LINE.fullmatch(raw.removesuffix(b"\n").removesuffix(b"\r"))
    if not match:
        return None
    try:
        return match[1] if isinstance(json.loads(match[1]), dict) else None
    except (ValueError, RecursionError):  # deep nesting raises the second
        return None


def save_event(path: Path, event: bytes) -> None:
    """Append one event in one write, so it never lands half; none once the file has
    reached SESSION_MAX_BYTES."""
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o640)
    try:
        if os.fstat(fd).st_size + len(event) < SESSION_MAX_BYTES:
            os.write(fd, event + b"\n")
    finally:
        os.close(fd)


def read_console(
    stream: IO[bytes], keep: Callable[[dict[str, Any]], None] | None, session: Path | None = None
) -> None:
    """Read a VM's console until it ends, keeping each valid live line and saving each
    session event to `session`. It must never stop early: once the pipe fills, the VM's
    console, and with it the job, would stall; so a failed save is logged once and reading
    goes on. A line over CONSOLE_LINE_MAX bytes is dropped, to its end, without being held
    in memory."""
    with stream:
        too_long, failed = False, False
        while chunk := stream.readline(CONSOLE_LINE_MAX):
            whole = chunk.endswith(b"\n")
            fits = not too_long and (whole or len(chunk) < CONSOLE_LINE_MAX)
            if keep and fits and (line := live_line(chunk, time.time())):
                keep(line)
            elif session and fits:
                try:
                    if event := session_event(chunk):
                        save_event(session, event)
                except Exception as e:  # anything, even a bug: the reader must outlive the VM
                    if not failed:
                        log(f"could not save to {session}: {type(e).__name__}: {e}")
                    failed = True
            too_long = not whole


def prune_sessions(folder: Path, now: float) -> None:
    """Delete session files not written to for SESSION_KEEP_SECONDS."""
    for path in folder.glob("*.jsonl"):
        try:
            if path.stat().st_mtime < now - SESSION_KEEP_SECONDS:
                path.unlink(missing_ok=True)
        except OSError:
            continue  # gone meanwhile, or tried again next poll


def vm_size(running: int) -> tuple[int, int]:
    """A job alone gets the whole machine's cores; with others running, each gets half."""
    return (4, 4096) if running == 0 else (2, 2048)


def next_size(state: State, standby: bool) -> tuple[int, int]:
    """A standby outlives the jobs around it, so it is sized for running alone. Light VMs
    are small and mostly wait, so they do not count."""
    return vm_size(0 if standby else sum(not run.light for run in state.launches.values()))


def mem_available_mib() -> int:
    """MemAvailable from /proc/meminfo, in MiB."""
    for line in MEMINFO.read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise ValueError(f"no MemAvailable in {MEMINFO}")


def disk_wait_percent() -> float:
    """How much of the last minute some task waited on the disk, in percent (0 if unknown)."""
    try:
        some = IO_PRESSURE.read_text().splitlines()[0].split()
        return float(dict(f.split("=") for f in some[1:])["avg60"])
    except (OSError, IndexError, KeyError, ValueError):
        return 0.0


def memory_reserve_mib() -> int:
    try:
        return max(0, int(MEMORY_RESERVE_FILE.read_text().strip()))
    except (OSError, ValueError):
        return DEFAULT_MEMORY_RESERVE_MIB


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


def has_job(gh: GitHub, run: Launch) -> bool:
    """GitHub says the VM's runner has taken a job (or has finished and gone)."""
    try:
        runner = gh.request("GET", f"/repos/{run.repo}/actions/runners/{run.runner_id}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return True
        raise
    return runner.get("busy") is True


def on_its_way(gh: GitHub, run: Launch, now: float, queued_for_want: int) -> bool:
    """The VM may still take a queued job of its repo and label: a standby, one that just
    took a job, or one starting whose runner has no job yet. GitHub is asked only while a
    job it could cover is queued, at most once a poll."""
    if run.standby or now < run.covers_until:
        return True
    if run.busy or now - run.started >= BOOT_GRACE_SECONDS:
        return False
    if queued_for_want:
        run.busy = has_job(gh, run)
    return not run.busy


def stop(run: Launch, why: str) -> None:
    log(f"slot {run.slot}: {run.runner_name} {why}, stopping it")
    subprocess.run(["systemctl", "stop", "--no-block", run.unit], check=False, timeout=30)
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
    running = running_vms()
    if running is None:
        return  # systemd did not answer: tried again next poll
    for slot, run in list(state.launches.items()):
        if run.unit in running:
            if queued is None:
                continue
            if run.standby:
                check_standby(gh, run, now, run.repo in private)
                if not run.standby:  # it took a job
                    state.used.add(run.repo)
                    write_status(
                        run.record, record(run)
                    )  # a restart must not take it for a standby
            else:
                stop_idle(gh, run, now, queued.get(run.want, 0))
            continue
        forget(run)
        delete_runner(gh, run.repo, run.runner_id, run.runner_name)
        log(f"slot {slot}: {run.runner_name} finished")
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


def remove_stale_runners(gh: GitHub, repos: Iterable[str], keep: Iterable[str] = ()) -> list[str]:
    """Registrations left by a previous supervisor whose VMs have ended (`keep` names the
    ones still running). Returns the repos GitHub would not show (say, the token cannot see
    them), to try again later."""
    keep = set(keep)
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
            if (
                name.startswith(RUNNER_PREFIX)
                and name not in keep
                and delete_runner(gh, repo, int(runner["id"]), name)
            ):
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
    # tar_filter checks a hard link's own path but not its target's: through a symlink
    # unpacked earlier, the target could be a host file, which the chown would then take.
    if member.islnk():
        root = os.path.realpath(dest)
        target = os.path.realpath(os.path.join(root, member.linkname))
        if os.path.commonpath([root, target]) != root:
            raise tarfile.FilterError(f"hard link to outside the folder: {member.name}")
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


def unpack_cache(stream: IO[bytes], root: Path) -> None:
    """Unpack a cache tar into root through size_capped. A file or link the archive holds
    twice takes its later entry, as tar does; tarfile would instead seek back for a hard link
    onto a file already there, which a stream cannot (servo's cache, 5 Nov)."""
    check = size_capped(CACHE_MAX_UNPACKED_BYTES)
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            safe = check(member, str(root))  # the path is inside root, symlinks resolved
            if safe is None:
                continue
            dest = root / safe.name
            if not safe.isdir() and os.path.lexists(dest) and not os.path.isdir(dest):
                dest.unlink()
            tar.extract(safe, root, filter="fully_trusted")


class BadArtifact(Exception):
    """The artifact itself cannot be unpacked; trying it again would fail the same way."""


def build_cache_image(zip_path: Path, image: Path) -> None:
    """Artifact zip -> cache.tar -> a folder -> an ext4 image, owned by the VM's runner user.
    Runs as root on the host and never executes anything from the artifact."""
    with tempfile.TemporaryDirectory(dir=CACHE_DIR) as tmp:
        root = Path(tmp) / "root"
        root.mkdir()
        try:
            with zipfile.ZipFile(zip_path) as z, z.open("cache.tar") as raw:
                unpack_cache(raw, root)
        except Exception as e:
            raise BadArtifact(f"{type(e).__name__}: {e}") from e
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
    if artifact["id"] in (known.get("artifact"), known.get("failed")):
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
        try:
            build_cache_image(zip_path, image)
        except BadArtifact as e:
            # The same artifact fails the same way: wait for a newer one rather than
            # download and unpack it again every minute.
            seen.write_text(json.dumps({**known, "failed": artifact["id"]}))
            log(f"cache for {repo}: artifact {artifact['id']} failed to unpack, {e}; ignored")
            return
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
                try:
                    refresh_cache(own, repo, branches[repo])
                except Exception as e:  # one repo's failure must not hold up the rest
                    log(f"cache refresh for {repo} failed: {type(e).__name__}: {e}")
        finally:
            state.cache_busy.release()

    threading.Thread(target=work, daemon=True).start()


def ask(gh: GitHub, state: State, repo: str, now: float) -> tuple[list[Job] | None, float]:
    """One repo's queued jobs (None if it is not served this poll), and the seconds the
    asking took. Run for every polled repo at once, each on its own thread."""
    asked = time.monotonic()
    # A repo that is no longer private has nothing queued as far as we are
    # concerned, so its idle VMs are stopped and no new ones start. One
    # repo GitHub will not show is skipped this poll; it never stops the others
    # (3 Nov: a repo the token could not see stopped all CI).
    try:
        if not check_private(gh, state, repo, now):
            skipped(repo, None)
            return None, time.monotonic() - asked
        jobs = find_queued(gh, repo)
        # A repo about to get its first VM is asked about its privacy directly.
        if (
            jobs
            and repo not in state.used
            and not (check_private(gh, state, repo, now, fresh=True) and only_colin(gh, repo))
        ):
            return None, time.monotonic() - asked
        skipped(repo, None)
        return jobs, time.monotonic() - asked
    except RateLimited:
        raise
    except Exception as e:
        skipped(repo, e)
        return None, time.monotonic() - asked


def tick(gh: GitHub, state: State, repos: list[str], now: float) -> dict[str, float]:
    """One poll. Returns how long asking each repo for queued jobs took, in seconds."""
    queued: Counter[Want] = Counter()
    job_ids: dict[Want, list[int]] = {}
    private: list[str] = []
    spent: dict[str, float] = {}
    # All at once: one after another, 9 repos took 13 s a poll while a job waited (4 Nov).
    answers = state.askers.map(lambda repo: ask(gh, state, repo, now), repos)
    for repo, (jobs, took) in zip(repos, answers, strict=True):
        spent[repo] = took
        if jobs is None:
            continue
        private.append(repo)
        queued.update(job.want for job in jobs)
        for job in jobs:
            job_ids.setdefault(job.want, []).append(job.id)
    reap(gh, state, now, queued, private)
    # Cache disks only for repos that run here; a quiet repo gets checked after its first job.
    maybe_refresh_caches(
        gh, state, [r for r in private if r in state.used or cache_image(r).exists()], now
    )
    # A VM still on its way to a job, or a standby waiting for one, covers a queued job
    # with the same repo and cache label (or none).
    booting: Counter[Want] = Counter(
        run.want for run in state.launches.values() if on_its_way(gh, run, now, queued[run.want])
    )
    free = [slot for slot in range(state.slots) if slot not in state.launches]
    light_free = [
        slot
        for slot in range(state.slots, state.slots + state.light_slots)
        if slot not in state.launches
    ]
    # GitHub's job ids only grow, so the smallest is the oldest.
    oldest_first = [w for _, w in sorted((i, w) for w, ids in job_ids.items() for i in ids)]
    starts = plan([w for w in oldest_first if w[1] != LIGHT_LABEL], booting, len(free))
    light_starts = plan([w for w in oldest_first if w[1] == LIGHT_LABEL], booting, len(light_free))
    # The VMs started this poll are counted by their size: they have touched almost
    # nothing yet, so MemAvailable does not show them. Builds first, then light jobs.
    room = mem_available_mib() - memory_reserve_mib()
    disk_wait = disk_wait_percent()
    disk_paced = disk_wait > DISK_WAIT_LIMIT / 2
    disk_busy = disk_wait > DISK_WAIT_LIMIT or (disk_paced and now < state.disk_paced_until)
    if disk_busy:
        room = min(room, 0)

    def pace() -> None:
        """One VM a minute while the disk is half busy: no more room after this start."""
        nonlocal room
        if disk_paced:
            state.disk_paced_until = now + DISK_PACE_SECONDS
            room = min(room, 0)

    started = 0
    for slot, (repo, label) in zip(free, starts, strict=False):
        mem = next_size(state, False)[1]
        if mem > room:
            break
        launch(gh, state, repo, slot, now, label=label, cache=cache_for(repo, label, private))
        room -= mem
        started += 1
        pace()
    light_started = 0
    for slot, (repo, label) in zip(light_free, light_starts, strict=False):
        # A build held for memory goes first: no light VM takes the memory it waits for.
        if started < len(starts) or LIGHT_SIZE[1] > room:
            break
        launch(gh, state, repo, slot, now, label=label, cache=None)
        room -= LIGHT_SIZE[1]
        light_started += 1
        pace()
    launched = starts[:started] + light_starts[:light_started]
    hold_for_memory(
        state,
        repos,
        starts[started:] + light_starts[light_started:],
        job_ids,
        booting + Counter(launched),
        "a busy disk" if disk_busy or disk_paced else "memory",
    )
    free = free[started:]
    # A build job still waiting for a slot, or for memory, gets the standby's slot and
    # memory, if the standby cannot take it. A light job never uses a build slot.
    waiting = queued - booting
    waiting.subtract(launched)
    for run in list(state.launches.values()):
        if run.standby and any(
            n > 0 and want != run.want and want[1] != LIGHT_LABEL for want, n in waiting.items()
        ):
            stop_if_still_idle(gh, run, "is in the way of a queued job")
    # A standby is the first thing given up: never booted while a job waits for memory.
    if (
        not state.held
        and next_size(state, True)[1] <= room
        and wants_standby(
            repos, private, [r.standby for r in state.launches.values()], len(free), standby_count()
        )
    ):
        repo = repos[0]
        # Booted for the last job's cache label too, else a job asking for it never takes
        # the standby and stops it as in the way (5 Nov: every task's Builder and Reviewer).
        label = state.last_label if repo == state.last_repo else ""
        cache = cache_for(repo, label, private)
        launch(gh, state, repo, free[0], now, label=label, cache=cache, standby=True)
        pace()
    return spent


def hold_for_memory(
    state: State,
    repos: list[str],
    held: list[Want],
    job_ids: Mapping[Want, list[int]],
    covered: Mapping[Want, int],
    why: str = "memory",
) -> None:
    """Note the queued jobs left waiting for memory or the disk, logging each once. They stay queued
    and are tried again next poll. Jobs of repos not polled this time keep their mark."""
    now_held: dict[int, str] = {}
    for want, n in Counter(held).items():
        skip = covered.get(want, 0)
        for job_id in sorted(job_ids.get(want, []))[skip : skip + n]:
            now_held[job_id] = want[0]
            if job_id not in state.held:
                log(f"job {job_id} in {want[0]} waits for {why}")
    state.held = {i: r for i, r in state.held.items() if r not in repos} | now_held


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


def light_slot_count() -> int:
    """The host's light slot count: 0 (none) if the file is missing or not a number."""
    try:
        text = LIGHT_SLOTS_FILE.read_text().strip()
    except OSError:
        return DEFAULT_LIGHT_SLOTS
    return int(text) if text.isdecimal() else DEFAULT_LIGHT_SLOTS


def slot_count() -> int:
    """The host's slot count: DEFAULT_SLOTS if the file is missing or not a number, at least 1."""
    try:
        text = SLOTS_FILE.read_text().strip()
    except OSError:
        return DEFAULT_SLOTS
    return max(1, int(text)) if text.isdecimal() else DEFAULT_SLOTS


def status(state: State, now: float, github_ok: bool, mem_available: int) -> dict[str, Any]:
    """Slots, VMs running a job or waiting ready, the repos with a cache disk, and the
    host's free memory with the jobs waiting for it."""
    standby = sum(run.standby for run in state.launches.values())
    light = sum(run.light for run in state.launches.values())
    return {
        "updated": int(now),
        "slots": state.slots,
        "in_use": len(state.launches) - standby - light,
        "standby": standby,
        "light_slots": state.light_slots,
        "light_in_use": light,
        "cache_disks": sorted(
            image.stem.replace("__", "/", 1) for image in CACHE_DIR.glob("*__*.ext4")
        ),
        "github_ok": github_ok,
        "mem_available_mib": mem_available,
        "held_for_memory": len(state.held),
    }


def read_live(run: Launch) -> dict[str, Any] | None:
    """The VM's latest live line, if its console reader has written one."""
    try:
        line = json.loads(run.live_file.read_text())
    except (OSError, ValueError):
        return None
    return line if isinstance(line, dict) else None


def live(state: State, now: float) -> dict[str, Any]:
    """The latest live line of each running VM that has sent one."""
    lines = [line for _, run in sorted(state.launches.items()) if (line := read_live(run))]
    return {"updated": int(now), "lines": lines}


def write_status(path: Path, data: Mapping[str, Any]) -> None:
    """Replace the file whole, so a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o755)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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


def list_repos(gh: GitHub) -> tuple[dict[str, str], list[str]]:
    """The served private repos with their default branches, and those nobody else can
    push to: the ones to serve."""
    listing = served_repos(gh)
    return listing, [r for r in listing if only_colin(gh, r)]


def slow_poll(took: float, requests: int, waited: float, spent: Mapping[str, float]) -> str:
    """What a slow poll spent its time on: GitHub's answers (summed over the threads),
    asking the repos for queued jobs at once (as long as the slowest, named), and the rest
    (stale runners, stopping and starting VMs)."""
    asking = max(spent.values(), default=0.0)
    slowest = sorted(spent.items(), key=lambda item: -item[1])[:3]
    named = ", ".join(f"{repo} {seconds:.1f}s" for repo, seconds in slowest)
    return (
        f"slow poll: {took:.1f}s; {requests} GitHub requests took {waited:.1f}s in all; "
        f"asking {len(spent)} repos for queued jobs at once took {asking:.1f}s"
        f"{f' (slowest {named})' if named else ''}; the rest {took - asking:.1f}s"
    )


def due(repos: list[str], state: State, now: float) -> list[str]:
    """The repos to poll now, in the order given: every one that has had a VM or has a
    cache disk, and of the quiet rest those unasked for a minute, QUIET_PER_POLL at most,
    the longest unasked first, so no poll is slowed by all of them at once."""
    busy = {r for r in repos if r in state.used or cache_image(r).exists()}
    quiet = [
        r for r in repos if r not in busy and now - state.polled.get(r, 0) >= QUIET_POLL_SECONDS
    ]
    quiet.sort(key=lambda r: state.polled.get(r, 0))
    chosen = busy | set(quiet[:QUIET_PER_POLL])
    return [r for r in repos if r in chosen]


def standby_first(repos: list[str], last: str) -> list[str]:
    """The repo of the last job first, so the standby VM waits where work just was."""
    return [last, *(r for r in repos if r != last)] if last in repos else repos


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["vm"]:
        return vm_main(argv[1:])
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
    state = State(slots=slot_count(), light_slots=light_slot_count())
    # The standby waits where the last job before the restart ran, not on the first repo
    # listed (4 Nov: it booted for a repo with no work, for 45 minutes).
    state.last_repo, state.last_label = recalled_last_job()
    if state.last_repo:
        state.used.add(state.last_repo)
    # The repo listing runs beside the poll, on its own client (the request cache is not
    # shared between threads).
    lister = ThreadPoolExecutor(max_workers=1)
    list_gh = gh.clone()
    listing: Future[tuple[dict[str, str], list[str]]] | None = None
    # Repos whose stale registrations are not cleared yet.
    unclean: list[str] = []
    failures = 0
    # Why the status file could not be written last time, so a lasting failure logs once.
    status_error = ""
    log(f"{state.slots} slots; serving private repos the token can see")
    # VMs started before a restart are still running: take them back before anything else.
    adopt(state)
    while True:
        wait: float = POLL_SECONDS
        github_ok = False
        # Any failure is logged and retried; exiting would kill the running jobs.
        try:
            if listing is None and time.time() - listed >= REPOS_EVERY:
                listed = time.time()
                listing = lister.submit(list_repos, list_gh)
            # The first listing is waited for: there is nothing to serve before it.
            if listing and (listing.done() or not repos):
                done, listing = listing, None
                # A failed listing keeps the last list (none at start), never ending the poll.
                try:
                    privacy, found = done.result()
                    # Listed private just now: quiet repos need no privacy request of their own.
                    for repo, branch in privacy.items():
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
            started, asked, waited = time.monotonic(), gh.requests, gh.seconds
            if unclean:
                running = [run.runner_name for run in state.launches.values()]
                unclean = remove_stale_runners(gh, unclean, running)
            now = time.time()
            polling = due(standby_first(repos, state.last_repo), state, now)
            spent = tick(gh, state, polling, now)
            for repo in polling:
                state.polled[repo] = now
            if (took := time.monotonic() - started) >= SLOW_POLL_SECONDS:
                log(slow_poll(took, gh.requests - asked, gh.seconds - waited, spent))
            failures = 0
            github_ok = True
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
        # Written after a failed poll too, so it still shows the supervisor is alive.
        try:
            write_status(STATUS_FILE, status(state, time.time(), github_ok, mem_available_mib()))
            write_status(LIVE_FILE, live(state, time.time()))
            prune_sessions(SESSIONS_DIR, time.time())
            status_error = ""
        except Exception as e:
            why = f"{type(e).__name__}: {e}"
            if why != status_error:
                log(f"could not write the status file: {why}")
            status_error = why
        if args.once:
            while state.launches:
                time.sleep(POLL_SECONDS)
                reap(gh, state, time.time())
            return 0
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
