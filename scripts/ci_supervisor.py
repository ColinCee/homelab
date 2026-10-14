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
API = "https://api.github.com"
SLOTS = 2
POLL_SECONDS = 10
# A VM that has not picked up a job by now counts as idle demand again.
BOOT_GRACE_SECONDS = 120
# After this, a VM that has not picked up a job is stopped (checked once a minute).
IDLE_AFTER_SECONDS = 180
IDLE_CHECK_SECONDS = 60
VM_TIMEOUT_SECONDS = 3600
PRIVACY_RECHECK_SECONDS = 300
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
    idle_checked: float = 0.0


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

    def get_jobs(run_id: int) -> Any:
        page = gh.request(
            "GET", f"/repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"
        )
        return page.get("jobs", [])

    return queued_jobs(repo, runs, get_jobs)


def delete_runner(gh: GitHub, repo: str, runner_id: int, name: str) -> None:
    try:
        gh.request("DELETE", f"/repos/{repo}/actions/runners/{runner_id}")
    except urllib.error.HTTPError as e:
        if e.code != 404:  # 404: the runner removed itself after its job
            log(f"could not remove runner {name} from {repo}: HTTP {e.code}")


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


def stop_idle(gh: GitHub, run: Launch, now: float) -> None:
    """A VM whose job was cancelled or taken by another runner would wait out its
    timeout; stop it once it has had time to boot and is still not busy."""
    if now - run.started < IDLE_AFTER_SECONDS or now - run.idle_checked < IDLE_CHECK_SECONDS:
        return
    run.idle_checked = now
    try:
        runner = gh.request("GET", f"/repos/{run.repo}/actions/runners/{run.runner_id}")
    except urllib.error.HTTPError as e:
        if e.code == 404:  # finished and removed itself; the VM is ending
            return
        raise
    if runner.get("busy") is False:
        log(f"slot {run.slot}: {run.runner_name} has no job, stopping it")
        run.process.terminate()


def reap(gh: GitHub, state: State, now: float) -> None:
    for slot, run in list(state.launches.items()):
        code = run.process.poll()
        if code is None:
            stop_idle(gh, run, now)
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
            if name.startswith(RUNNER_PREFIX):
                delete_runner(gh, repo, int(runner["id"]), name)
                log(f"removed stale runner {name} from {repo}")


def tick(gh: GitHub, state: State, repos: list[str], now: float) -> None:
    reap(gh, state, now)
    queued: dict[str, int] = {}
    for repo in repos:
        if check_private(gh, state, repo, now):
            count = len(find_queued(gh, repo))
            if count:
                queued[repo] = count
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
