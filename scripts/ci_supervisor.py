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


class GitHub:
    def __init__(self, token: str) -> None:
        self._token = token

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"{API}{path}", data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
        return json.loads(raw) if raw else None


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


def launch(gh: GitHub, state: State, repo: str, slot: int, now: float) -> None:
    name = f"{RUNNER_PREFIX}{slot}-{int(now)}"
    registration = gh.request(
        "POST",
        f"/repos/{repo}/actions/runners/generate-jitconfig",
        {"name": name, "runner_group_id": 1, "labels": list(RUNNER_LABELS), "work_folder": "_work"},
    )
    metadata = STATE_DIR / f"metadata-slot{slot}.json"
    fd = os.open(metadata, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"task": "runner", "jitconfig": registration["encoded_jit_config"]}, f)
    process = subprocess.Popen(
        [str(RUN_VM), str(slot), str(metadata), str(VM_TIMEOUT_SECONDS)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    runner_id = int(registration["runner"]["id"])
    state.launches[slot] = Launch(slot, repo, runner_id, name, now, process, metadata)
    log(f"slot {slot}: started {name} for {repo}")


def reap(gh: GitHub, state: State) -> None:
    for slot, run in list(state.launches.items()):
        code = run.process.poll()
        if code is None:
            continue
        run.metadata.unlink(missing_ok=True)
        try:
            gh.request("DELETE", f"/repos/{run.repo}/actions/runners/{run.runner_id}")
        except urllib.error.HTTPError as e:
            if e.code != 404:  # 404: the runner removed itself after its job
                log(f"slot {slot}: could not remove runner {run.runner_name}: {e.code}")
        log(f"slot {slot}: {run.runner_name} finished (exit {code})")
        del state.launches[slot]


def remove_stale_runners(gh: GitHub, repos: Iterable[str]) -> None:
    """Registrations left by a previous supervisor; their VMs died with it."""
    for repo in repos:
        page = gh.request("GET", f"/repos/{repo}/actions/runners?per_page=100")
        for runner in page.get("runners", []):
            if str(runner.get("name", "")).startswith(RUNNER_PREFIX):
                gh.request("DELETE", f"/repos/{repo}/actions/runners/{runner['id']}")
                log(f"removed stale runner {runner['name']} from {repo}")


def tick(gh: GitHub, state: State, repos: list[str], now: float) -> None:
    reap(gh, state)
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
    remove_stale_runners(gh, repos)
    log(f"watching {len(repos)} repositories, {SLOTS} slots")
    while True:
        try:
            tick(gh, state, repos, time.time())
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
            log(f"poll failed: {e}")
        if args.once:
            while state.launches:
                time.sleep(POLL_SECONDS)
                reap(gh, state)
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
