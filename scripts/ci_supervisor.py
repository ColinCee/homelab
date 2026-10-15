#!/usr/bin/env python3
"""Start a throwaway Firecracker VM for each queued CI job on Colin's private repos.

Polls GitHub for queued jobs asking for the `homelab-fc` label, on the repositories
listed in /etc/homelab-ci/repos. For each one it asks GitHub for a just-in-time runner
registration (good for exactly one job) and boots a VM with run-vm.sh, which receives
that registration through Firecracker's metadata service. The GitHub token stays on
the host; a VM only ever sees its one-job registration. A repository that is not
private, or not on the list, is never served. See docs/runbooks/ci-runner.md.

Installed root-owned by ci-runner/install-host.sh and run by homelab-ci-supervisor.service.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LABEL = "homelab-fc"
RUNNER_LABELS = ("self-hosted", "linux", "x64", LABEL)
REPOS_FILE = Path("/etc/homelab-ci/repos")
TOKEN_FILE = Path("/etc/homelab-ci/github-token")
RUN_VM = Path("/usr/local/lib/homelab-ci/run-vm.sh")
STATE_DIR = Path("/run/homelab-ci")
CGROUP_ROOT = Path("/sys/fs/cgroup/firecracker")
API = "https://api.github.com"
SLOTS = 2
POLL_SECONDS = 10
# A VM that has not picked up a job by now counts as idle demand again.
BOOT_GRACE_SECONDS = 120
# Time for a VM to boot and connect. After it, a VM with no job while nothing is
# queued for its repo (the job was cancelled, or another runner took it) is stopped.
IDLE_AFTER_SECONDS = 30
VM_TIMEOUT_SECONDS = 3600
# Checked every poll: conditional requests make an unchanged answer free.
PRIVACY_RECHECK_SECONDS = 0
RUNNER_PREFIX = "fc-"


@dataclass(frozen=True)
class Job:
    repo: str
    id: int
    name: str
    labels: tuple[str, ...]


@dataclass
class Launch:
    slot: int
    repo: str
    runner_id: int
    runner_name: str
    started: float
    process: subprocess.Popen[bytes]
    metadata: Path


@dataclass
class Privacy:
    private: bool
    checked: float


@dataclass
class State:
    launches: dict[int, Launch] = field(default_factory=dict)
    privacy: dict[str, Privacy] = field(default_factory=dict)


def log(message: str) -> None:
    print(message, flush=True)


def read_repos(text: str) -> list[str]:
    """owner/name per line; blank lines and # comments ignored."""
    repos: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        owner, sep, name = line.partition("/")
        if not sep or not owner or not name or "/" in name:
            raise ValueError(f"not owner/name: {line!r}")
        repos.append(line)
    return repos


def wants_us(labels: Iterable[str]) -> bool:
    """A job is ours if it asks for our label and nothing our VMs do not have."""
    asked = {label.lower() for label in labels}
    return LABEL in asked and asked <= set(RUNNER_LABELS)


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


def plan(queued: Mapping[str, int], booting: Mapping[str, int], free_slots: int) -> list[str]:
    """Which repos get a new VM now: one per queued job not already covered by a booting VM."""
    starts: list[str] = []
    for repo in sorted(queued):
        need = max(0, queued[repo] - booting.get(repo, 0))
        for _ in range(need):
            if len(starts) == free_slots:
                return starts
            starts.append(repo)
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

    def forget(self, drop: Callable[[str], bool]) -> None:
        for path in [path for path in self._cache if drop(path)]:
            del self._cache[path]


def rate_limit_until(retry_after: str | None, reset: str | None, now: float) -> float:
    """When to try again: Retry-After seconds, else the reset time, else a minute."""
    if retry_after:
        return now + int(retry_after)
    return float(reset) if reset else now + 60


def check_private(gh: GitHub, state: State, repo: str, now: float) -> bool:
    known = state.privacy.get(repo)
    if known is None or now - known.checked > PRIVACY_RECHECK_SECONDS:
        private = is_private(gh.request("GET", f"/repos/{repo}"), repo)
        if not private:
            log(f"refusing {repo}: not a private repository")
        state.privacy[repo] = Privacy(private, now)
    return state.privacy[repo].private


def find_queued(gh: GitHub, repo: str) -> list[Job]:
    runs: list[Mapping[str, Any]] = []
    # A multi-job run is in_progress while its later jobs wait for a runner.
    for status in ("queued", "in_progress"):
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


def launch(gh: GitHub, state: State, repo: str, slot: int, now: float) -> None:
    name = f"{RUNNER_PREFIX}{slot}-{int(now)}"
    registration = gh.request(
        "POST",
        f"/repos/{repo}/actions/runners/generate-jitconfig",
        {"name": name, "runner_group_id": 1, "labels": list(RUNNER_LABELS), "work_folder": "_work"},
    )
    runner_id = int(registration["runner"]["id"])
    metadata = STATE_DIR / f"metadata-slot{slot}.json"
    try:
        fd = os.open(metadata, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"task": "runner", "jitconfig": registration["encoded_jit_config"]}, f)
        process = subprocess.Popen(
            [str(RUN_VM), str(slot), str(metadata), str(VM_TIMEOUT_SECONDS)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except BaseException:
        metadata.unlink(missing_ok=True)
        delete_runner(gh, repo, runner_id, name)
        raise
    state.launches[slot] = Launch(slot, repo, runner_id, name, now, process, metadata)
    log(f"slot {slot}: started {name} for {repo}")


def may_be_idle(age: float, queued_for_repo: int) -> bool:
    """Worth asking GitHub whether the VM has a job: it has had time to connect,
    and no queued job in its repo could still be on its way to it."""
    return age >= IDLE_AFTER_SECONDS and queued_for_repo == 0


def stop_idle(gh: GitHub, run: Launch, now: float, queued_for_repo: int) -> None:
    """A VM whose job was cancelled or taken by another runner would wait out its
    timeout; stop it as soon as it is clearly not going to get one."""
    if not may_be_idle(now - run.started, queued_for_repo):
        return

    def idle() -> bool:
        try:
            runner = gh.request("GET", f"/repos/{run.repo}/actions/runners/{run.runner_id}")
        except urllib.error.HTTPError as e:
            if e.code == 404:  # finished and removed itself; the VM is ending
                return False
            raise
        return runner.get("busy") is False

    # Asked twice, so a job assigned between the two answers is not killed.
    if not idle():
        return
    time.sleep(2)
    if idle():
        log(f"slot {run.slot}: {run.runner_name} has no job, stopping it")
        run.process.terminate()
        # Do not rely on run-vm.sh's trap alone: the VM lives in its own cgroup.
        kill = CGROUP_ROOT / f"ci-slot{run.slot}" / "cgroup.kill"
        if kill.exists():
            kill.write_text("1")


def reap(gh: GitHub, state: State, now: float, queued: Mapping[str, int] | None = None) -> None:
    """Clear finished VMs. With this poll's queue, also stop VMs left without a job."""
    for slot, run in list(state.launches.items()):
        code = run.process.poll()
        if code is None:
            if queued is not None:
                stop_idle(gh, run, now, queued.get(run.repo, 0))
            continue
        run.metadata.unlink(missing_ok=True)
        delete_runner(gh, run.repo, run.runner_id, run.runner_name)
        log(f"slot {slot}: {run.runner_name} finished (exit {code})")
        del state.launches[slot]


def remove_stale_runners(gh: GitHub, repos: Iterable[str]) -> None:
    """Registrations left by a previous supervisor; their VMs died with it."""
    for repo in repos:
        page = gh.request("GET", f"/repos/{repo}/actions/runners?per_page=100")
        for runner in page.get("runners", []):
            name = str(runner.get("name", ""))
            if name.startswith(RUNNER_PREFIX) and delete_runner(gh, repo, int(runner["id"]), name):
                log(f"removed stale runner {name} from {repo}")


def tick(gh: GitHub, state: State, repos: list[str], now: float) -> None:
    queued: dict[str, int] = {}
    for repo in repos:
        # A repo that is no longer private has nothing queued as far as we are
        # concerned, so its idle VMs are stopped below and no new ones start.
        if check_private(gh, state, repo, now):
            count = len(find_queued(gh, repo))
            if count:
                queued[repo] = count
    reap(gh, state, now, queued)
    booting: dict[str, int] = {}
    for run in state.launches.values():
        if now - run.started < BOOT_GRACE_SECONDS:
            booting[run.repo] = booting.get(run.repo, 0) + 1
    free = [slot for slot in range(SLOTS) if slot not in state.launches]
    for slot, repo in zip(free, plan(queued, booting, len(free)), strict=False):
        launch(gh, state, repo, slot, now)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="one poll, for testing")
    args = parser.parse_args(argv)
    repos = read_repos(REPOS_FILE.read_text())
    gh = GitHub(TOKEN_FILE.read_text().strip())
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    state = State()
    cleaned = False
    log(f"watching {len(repos)} repositories, {SLOTS} slots")
    while True:
        wait: float = POLL_SECONDS
        # Any failure is logged and retried; exiting would kill the running jobs.
        try:
            if not cleaned:
                remove_stale_runners(gh, repos)
                cleaned = True
            tick(gh, state, repos, time.time())
        except RateLimited as e:
            log(str(e))
            wait = max(POLL_SECONDS, e.until - time.time() + 1)
        except Exception as e:
            log(f"poll failed: {type(e).__name__}: {e}")
        if args.once:
            while state.launches:
                time.sleep(POLL_SECONDS)
                reap(gh, state, time.time())
            return 0
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
