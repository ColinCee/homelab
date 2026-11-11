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
import contextlib
import fcntl
import hashlib
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
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any

LABEL = "homelab-fc"
RUNNER_LABELS = ("self-hosted", "linux", "x64", LABEL)
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
# Build VMs are kept booted and registered for the jobs likely next (see standby_wants), so
# a job starts in seconds instead of waiting for a VM. Each is replaced every 45 minutes
# while idle; its timeout leaves a job that starts just before then time to finish.
STANDBY_RECYCLE_SECONDS = 45 * 60
# How many build VMs wait ready at once (each about 450 MB while idle), from
# /etc/homelab-ci/standbys if present (see standby_count and standby_wants).
STANDBY_COUNT_FILE = Path("/etc/homelab-ci/standbys")
STANDBY_TIMEOUT_SECONDS = 2 * 3600
# Checked every poll: conditional requests make an unchanged answer free.
PRIVACY_RECHECK_SECONDS = 0
RUNNER_PREFIX = "fc-"
# Each repo's read-only cache disk: its package caches and installs, built from what a
# green push to its default branch leaves on its VM's outbox disk (see ci-runner.md).
CACHE_DIR = Path("/var/lib/homelab-ci/caches")
# / is shared with everything. run-vm.sh sizes the outbox to this and its header.
CACHE_MAX_UNPACKED_BYTES = 6 * 1024**3
# Each VM's outbox (<runner name>.tar) and the repo it ran for (<runner name>.json), kept
# after the VM until judged. Not through GitHub artifacts: they count against the account's
# storage quota, and once it is full every upload fails.
OUTBOX_DIR = Path("/var/lib/homelab-ci/outbox")
# What the fc-cache action writes last, once the tar after it is complete.
OUTBOX_HEADER = re.compile(rb"homelab-ci-cache v1 run=([0-9]{1,20}) attempt=([0-9]{1,5})\n")
# An outbox whose job GitHub does not show finished yet is tried again a minute later,
# for up to an hour.
OUTBOX_RETRY_SECONDS = 60
OUTBOX_WAIT_SECONDS = 3600
RUNNER_UID = 1001  # the runner user inside the VM image
# What the CI runner is doing: rewritten after every poll, readable by anyone on the host.
# Nothing secret goes in it (see status()).
STATUS_FILE = Path("/var/lib/homelab-ci/status/machines.json")
CONSOLE_LINE_MAX = 4096
# What the job really needed (cache left out), the VM's memory and the kernel's out-of-memory
# kills, from the VM's ci-memwatch. A job can print it too: it only skews the counts.
MEM_LINE = re.compile(rb"HOMELAB-MEM 1 ([0-9]{1,7}) ([0-9]{1,7}) ([0-9]{1,9})")
# Counts by kind of VM for OpenObserve (Alloy's textfile collector), and the share of its memory a
# job may need before its kind counts as near full.
MEMORY_METRICS = Path("/var/lib/homelab-metrics/ci-vm-memory.prom")
NEAR_FULL = 0.8
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
# A queued job not started is logged with why (slots, memory, disk, labels) when it starts
# waiting and then every this many seconds, to explain a long wait.
WAIT_LOG_SECONDS = 180.0
# A job a VM is booting for, or one whose VM's runner is up and idle but GitHub has not
# given it the job, is logged only once it has waited WAIT_LOG_SECONDS, so every job's few
# seconds of boot are not logged.
BOOTING = "a VM booting for it"
READY = "a VM ready for it that GitHub has not given the job"
# A job GitHub had queued this long before the supervisor first saw it is logged then: the
# quiet repos are asked once a minute, so less is expected.
UNSEEN_LOG_SECONDS = 90.0
# A new run's first build job waits behind later jobs of started runs at most this long
# since GitHub queued it, then goes back to oldest first: a few minutes, never starved.
FIRST_BUILD_WAIT_MAX = 600.0
# Each job that waited, one JSON line when it stops waiting, with its seconds per kind of
# wait (see wait_kind) and the reason of its longest kind, for speed reports. Lines older
# than WAITS_KEEP_SECONDS are dropped at most every WAITS_PRUNE_SECONDS. "unseen" is the
# time from GitHub queueing the job to the supervisor first seeing it (a restart counts
# again from GitHub's queueing).
WAITS_FILE = STATUS_FILE.with_name("waits.jsonl")
WAIT_KINDS = ("memory", "disk", "slots", "booting", "ready", "unseen", "other")
WAITS_KEEP_SECONDS = 30 * 24 * 3600
WAITS_PRUNE_SECONDS = 3600.0
# The longest-waiting queued job's seconds so far per kind of wait, for OpenObserve (Alloy's
# textfile collector), at most every WAIT_METRICS_SECONDS: a write every 2 s poll would only
# add load to the shared disk.
WAIT_METRICS = MEMORY_METRICS.with_name("ci-waits.prom")
WAIT_METRICS_SECONDS = 15.0
# A VM is told how long each queued job of its repo and label had waited here, and why
# (the wait log's reason), at most this many, oldest first: ci-boot writes it to
# /etc/homelab-ci/waits.json for the job's own record.
VM_WAITS_MAX = 50
# The host's own load test (ci-runner/tests/test-load.sh), run by LOAD_TEST_UNIT once for each
# slot count and version of the test ("Changing the number of slots" in ci-runner.md). Its
# result goes in the status file and in each VM at /etc/homelab-ci/load-test.json.
LOAD_TEST_FILE = Path("/var/lib/homelab-ci/load-test.json")
LOAD_TEST_SCRIPT = Path("/usr/local/lib/homelab-ci/test-load.sh")
LOAD_TEST_UNIT = "homelab-ci-load-test.service"
# While this file exists no VM starts and the standbys stop, so the test has the host to itself.
# The unit removes it however it ends (ExecStopPost), and /run is empty after a reboot.
DRAIN_FILE = Path("/run/homelab-ci/load-test-drain")
# A job VM's own limit, and a little: every running job ends within it.
DRAIN_LIMIT_SECONDS = VM_TIMEOUT_SECONDS + 300
DRAIN_LOOK_SECONDS = 10
# A failed test lowers the slot count by one, never below this: fewer is Colin's call.
MIN_TESTED_SLOTS = 3
# A test that did not run (jobs outlasted the drain, or the script broke) is tried again after this.
LOAD_TEST_RETRY_SECONDS = 6 * 3600
# update.sh's lock: no install or image build adds load while the test runs.
UPDATE_LOCK = Path("/run/homelab-ci-update.lock")


# What a VM is for: its repo, and the extra cache label it registers with ("" for none).
# A VM covers only jobs with the same pair.
type Want = tuple[str, str]


@dataclass(frozen=True)
class Job:
    repo: str
    id: int
    name: str
    labels: tuple[str, ...]
    # False for a job asking for our label with others no VM of ours has: it never starts.
    ours: bool = True
    # When GitHub queued it (its created_at), 0 when unknown.
    queued_at: float = 0.0
    # Its workflow's name, "" when unknown.
    workflow: str = ""
    # Its run already finished a job on the build label (a task's review after its build).
    run_started: bool = False

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
    metadata: Path
    # Booted ahead of any job; becomes an ordinary job VM once GitHub gives it one.
    standby: bool = False
    # A standby that just took a job still covers it until GitHub's job list catches up.
    covers_until: float = 0.0
    label: str = ""
    # GitHub showed its runner with a job: it no longer covers a queued one.
    busy: bool = False
    # GitHub showed its runner online and idle: a job it covers waits on GitHub, not the boot.
    ready: bool = False

    @property
    def unit(self) -> str:
        return f"{UNIT_PREFIX}{self.runner_name}"

    @property
    def record(self) -> Path:
        return VMS_DIR / f"{self.runner_name}.json"

    @property
    def memory_file(self) -> Path:
        """Its latest memory line, written by its console reader (`vm` mode)."""
        return VMS_DIR / f"{self.runner_name}.mem"

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
    slots: int = DEFAULT_SLOTS
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
    # Queued jobs not started, with why (see WAIT_LOG_SECONDS).
    waits: dict[int, Wait] = field(default_factory=dict)
    # When WAITS_FILE was last pruned, and why it could not be written last time, so a
    # lasting failure logs once.
    waits_pruned: float = 0.0
    waits_error: str = ""
    # When WAIT_METRICS was last written, and why it could not be written last time.
    wait_metrics_written: float = float("-inf")
    wait_metrics_error: str = ""
    # Polls failed in a row (a rate limit is not one), and when the first of them failed.
    poll_failures: int = 0
    poll_failed_since: float | None = None
    # Whether the last poll found the load test's drain on (see DRAIN_FILE).
    draining: bool = False


@dataclass
class Wait:
    repo: str
    since: float
    # When GitHub queued the job, or `since` when unknown.
    queued: float = 0.0
    logged: float | None = None
    # The kind of wait at the last poll that saw it, and when: the time until the next
    # poll counts for that kind.
    kind: str = "other"
    seen: float = 0.0
    seconds: dict[str, float] = field(default_factory=dict)
    # The reason the wait log gives at the last poll that saw it, and the latest one for
    # each kind, so the finished record names the reason of its longest kind.
    why: str = ""
    whys: dict[str, str] = field(default_factory=dict)
    # Its workflow's name, "" when unknown.
    workflow: str = ""
    # The labels it asks for when no VM has them, else empty.
    unmatched: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.queued = self.queued or self.since

    def longest_why(self) -> str:
        """The wait log's reason for the kind it waited longest for ("" if none was logged)."""
        kinds = sorted(self.whys, key=lambda kind: self.seconds.get(kind, 0.0), reverse=True)
        return self.whys[kinds[0]] if kinds else self.why

    def count(self, now: float) -> None:
        self.seconds[self.kind] = self.seconds.get(self.kind, 0.0) + now - self.seen
        self.seen = now


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
        run_jobs = list(get_jobs(int(run["id"])))
        done = [job for job in run_jobs if job.get("status") == "completed"]
        run_started = any(on_build_label(job.get("labels", [])) for job in done)
        for job in run_jobs:
            labels = tuple(str(label) for label in job.get("labels", []))
            if job.get("status") != "queued":
                continue
            ours = wants_us(labels)
            # One asking for our label with another we lack is kept, to log why it waits.
            if ours or LABEL in {label.lower() for label in labels}:
                queued_at = github_time(job.get("created_at"))
                jobs.append(
                    Job(
                        repo,
                        int(job["id"]),
                        str(job.get("name", "")),
                        labels,
                        ours,
                        queued_at,
                        str(job.get("workflow_name") or run.get("name") or ""),
                        run_started,
                    )
                )
    return jobs


def on_build_label(labels: Iterable[str]) -> bool:
    return LABEL in {str(label).lower() for label in labels}


def start_order(jobs: Iterable[Job], waited_since: Callable[[int], float], now: float) -> list[Job]:
    """Jobs in the order they get slots and memory: oldest first, except that a new run's
    first build job waits behind later jobs of runs already started (a task's review), so a
    task half done is not left behind newer tasks' builds. A job queued over
    FIRST_BUILD_WAIT_MAX goes back to oldest first."""

    def waited(job: Job) -> float:
        return now - (job.queued_at or waited_since(job.id))

    def new(job: Job) -> bool:
        if job.run_started or not on_build_label(job.labels):
            return False
        return waited(job) < FIRST_BUILD_WAIT_MAX

    return sorted(jobs, key=lambda job: (new(job), job.id))


def github_time(value: Any) -> float:
    """Seconds since the epoch of one of GitHub's times, 0 when missing or unreadable."""
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return 0.0


def plan(in_order: Iterable[Want], booting: Mapping[Want, int], free_slots: int) -> list[Want]:
    """Which VMs start now: one per queued job (each given by its want, in start_order)
    not already covered by a booting VM. In job order, not by want, so a job wanting a rarer
    cache is not starved by a stream of newer ones (one sorted last waited 1h45)."""
    cover = Counter(booting)
    starts: list[Want] = []
    for want in in_order:
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
    jobs: Iterable[int] = (),
) -> None:
    """Start a VM for repo and label. `jobs` are the queued job ids it may take, whose
    waits so far go into its metadata."""
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
    vcpus, mem_mib = next_size(state, standby)
    timeout = STANDBY_TIMEOUT_SECONDS if standby else VM_TIMEOUT_SECONDS
    run = Launch(slot, repo, runner_id, name, now, metadata, standby, label=label)
    try:
        fd = os.open(metadata, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(
                {
                    "task": "runner",
                    "jitconfig": registration["encoded_jit_config"],
                    "waits": vm_waits(state, jobs, now),
                    "load_test": read_load_test() or {},
                },
                f,
            )
        # The record first: a supervisor that dies right after starting the VM still finds it.
        write_status(run.record, record(run))
        env = {"VCPUS": str(vcpus), "MEM_MIB": str(mem_mib), "CACHE_IMAGE": str(cache or "")}
        (OUTBOX_DIR / f"{name}.json").write_text(json.dumps({"repo": repo}))
        env["CACHE_OUT"] = str(OUTBOX_DIR / f"{name}.tar")
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
    if not standby:
        state.used.add(repo)
        note_last_job(state, repo, label)
    kind = "standby" if standby else "VM"
    disk = cache.name if cache else "no cache disk"
    log(f"slot {slot}: started {kind} {name} for {repo}, {disk} ({vcpus} vCPUs, {mem_mib} MiB)")


def vm_waits(state: State, jobs: Iterable[int], now: float) -> dict[str, dict[str, Any]]:
    """Each job's seconds waited here and why, oldest VM_WAITS_MAX first; a job first seen
    this poll has waited 0 s."""
    waits: dict[str, dict[str, Any]] = {}
    for job_id in sorted(jobs)[:VM_WAITS_MAX]:
        wait = state.waits.get(job_id)
        waits[str(job_id)] = {
            "seconds": int(now - wait.since) if wait else 0,
            "why": wait.why if wait else "",
        }
    return waits


def note_last_job(state: State, repo: str, label: str) -> None:
    """The repo and cache label the build standby boots for next."""
    if (repo, label) != (state.last_repo, state.last_label):
        state.last_repo, state.last_label = repo, label
        remember_last_job(repo, label)


def remember_last_job(repo: str, label: str) -> None:
    remember(LAST_REPO_FILE, f"{repo}\n{label}\n")


def remember(path: Path, text: str) -> None:
    """Kept for the next start; losing it only sends the first standby elsewhere."""
    try:
        path.write_text(text)
    except OSError as e:
        log(f"could not keep the last job's repo: {type(e).__name__}: {e}")


def recalled_repo(path: Path, default: str) -> str:
    """The repo kept in a last-repo file: its first line (older files add a label after it)."""
    try:
        lines = path.read_text().split()
    except OSError:
        return default
    return lines[0] if lines else default


def recalled_last_job() -> Want:
    """The last build job's repo and cache label ("" for none, as from an older file)."""
    try:
        lines = LAST_REPO_FILE.read_text().split()
    except OSError:
        return ("", "")
    return (lines[0] if lines else "", lines[1] if len(lines) > 1 else "")


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
    running. Its console goes through this script's `vm` mode, which keeps its memory line.
    The unit stops before the CI network does, at shutdown."""
    vm = [str(run.slot), str(run.metadata), str(timeout), "--memory", str(run.memory_file)]
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
    """A VM's files, once it has ended, its memory counted first."""
    count_memory(run)
    for path in (run.record, run.memory_file, run.metadata):
        path.unlink(missing_ok=True)


# Ended job VMs, near-full ones and out-of-memory kills, by kind; every kind from 0, so the
# first kill after a restart still shows as an increase.
MEMORY_COUNTS: dict[str, list[int]] = {"build": [0, 0, 0]}


def count_memory(run: Launch) -> None:
    """Log and count what an ended job VM needed. A standby that never took a job, or a VM
    whose console said nothing, is left out."""
    try:
        seen = json.loads(run.memory_file.read_text())
        peak, total, kills = int(seen["peak"]), int(seen["total"]), int(seen["kills"])
    except (OSError, ValueError, KeyError, TypeError):
        return
    if run.standby or total <= 0:
        return
    log(
        f"slot {run.slot}: {run.runner_name} (build) needed at least {peak} MiB of"
        f" {total}, {kills} out-of-memory kills"
    )
    counts = MEMORY_COUNTS["build"]
    counts[0] += 1
    counts[1] += peak > NEAR_FULL * total
    counts[2] += kills
    write_memory_metrics()


def write_memory_metrics() -> None:
    """Replace the textfile whole; a failed write only leaves the last counts."""
    names = (
        "homelab_ci_vms_ended_total",
        "homelab_ci_vms_near_full_total",
        "homelab_ci_vm_oom_kills_total",
    )
    lines = [
        f'{name}{{kind="{kind}"}} {counts[i]}'
        for i, name in enumerate(names)
        for kind, counts in MEMORY_COUNTS.items()
    ]
    try:
        write_textfile(MEMORY_METRICS, lines)
    except OSError as e:
        log(f"could not write {MEMORY_METRICS}: {type(e).__name__}: {e}")


def write_textfile(path: Path, lines: list[str]) -> None:
    """Replace a textfile for Alloy's collector whole, readable by anyone."""
    # Not *.prom, or the collector could read it half written.
    tmp = path.with_name(f".{path.stem}.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.chmod(0o644)
    os.replace(tmp, path)


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
    """`vm SLOT METADATA TIMEOUT [--memory FILE]`: run-vm.sh's console reader, the main process
    of the VM's unit. It reads the console to the end (a full pipe would stall the VM), keeps
    the latest memory line in FILE, and exits with run-vm.sh's code."""
    parser = argparse.ArgumentParser(prog="ci_supervisor.py vm")
    parser.add_argument("slot")
    parser.add_argument("metadata")
    parser.add_argument("timeout")
    parser.add_argument("--memory", type=Path)
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
    memory = (lambda seen: write_status(args.memory, seen)) if args.memory else None
    read_console(process.stdout, memory)
    return process.wait()


def memory_line(raw: bytes) -> dict[str, int] | None:
    """One console line as ci-memwatch's report, or None unless it is exactly that."""
    match = MEM_LINE.fullmatch(raw.removesuffix(b"\n").removesuffix(b"\r"))
    if not match:
        return None
    return {"peak": int(match[1]), "total": int(match[2]), "kills": int(match[3])}


def read_console(stream: IO[bytes], memory: Callable[[dict[str, int]], None] | None = None) -> None:
    """Read a VM's console until it ends, handing each memory line to `memory`. It must never
    stop early: once the pipe fills, the VM's console, and with it the job, would stall. A line
    over CONSOLE_LINE_MAX bytes is dropped, to its end, without being held in memory; the rest
    of the console is read and dropped, never logged."""
    with stream:
        too_long = False
        while chunk := stream.readline(CONSOLE_LINE_MAX):
            whole = chunk.endswith(b"\n")
            fits = not too_long and (whole or len(chunk) < CONSOLE_LINE_MAX)
            if memory and fits and (seen := memory_line(chunk)):
                memory(seen)
            too_long = not whole


def vm_size(running: int) -> tuple[int, int]:
    """A job alone gets the whole machine's cores; with others running, each gets half."""
    return (4, 4096) if running == 0 else (2, 2048)


def next_size(state: State, standby: bool) -> tuple[int, int]:
    """A standby outlives the jobs around it, so it is sized for running alone."""
    return vm_size(0 if standby else len(state.launches))


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
    note_ready(run, runner)
    return runner.get("busy") is False


def has_job(gh: GitHub, run: Launch) -> bool:
    """GitHub says the VM's runner has taken a job (or has finished and gone)."""
    try:
        runner = gh.request("GET", f"/repos/{run.repo}/actions/runners/{run.runner_id}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return True
        raise
    note_ready(run, runner)
    return runner.get("busy") is True


def note_ready(run: Launch, runner: Mapping[str, Any]) -> None:
    if runner.get("status") == "online" and runner.get("busy") is False:
        run.ready = True


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
                    # A restart must not take it for a standby.
                    with bookkeeping(run, "its record"):
                        write_status(run.record, record(run))
            else:
                stop_idle(gh, run, now, queued.get(run.want, 0))
            continue
        # One ended VM's bookkeeping failing is logged and its slot still freed: raised into
        # the poll, it would stop every job start until fixed.
        with bookkeeping(run, "bookkeeping"):
            forget(run)
        with bookkeeping(run, "bookkeeping"):
            delete_runner(gh, run.repo, run.runner_id, run.runner_name)
        # A task's next build job (its Reviewer after its Builder) asks for the same repo and
        # cache label seconds after this one ends: the standby booting in the freed slot is
        # for those, not for the last job started, so the next job need not wait for a boot.
        if not run.standby:
            with bookkeeping(run, "bookkeeping"):
                note_last_job(state, run.repo, run.label)
        log(f"slot {slot}: {run.runner_name} finished")
        del state.launches[slot]


@contextlib.contextmanager
def bookkeeping(run: Launch, what: str) -> Iterator[None]:
    """Logs a failure of one VM's after-the-fact bookkeeping instead of failing the poll. A
    rate limit still ends the poll, leaving the VM to be tried again on the next."""
    try:
        yield
    except RateLimited:
        raise
    except Exception as e:
        log(f"slot {run.slot}: {run.runner_name} {what} failed: {type(e).__name__}: {e}")


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


class BadCache(Exception):
    """The tar itself cannot be unpacked; trying it again would fail the same way."""


def build_cache_image(stream: IO[bytes], image: Path) -> None:
    """Cache tar -> a folder -> an ext4 image, owned by the VM's runner user.
    Runs as root on the host and never executes anything from the tar."""
    with tempfile.TemporaryDirectory(dir=CACHE_DIR) as tmp:
        root = Path(tmp) / "root"
        root.mkdir()
        try:
            unpack_cache(stream, root)
        except Exception as e:
            raise BadCache(f"{type(e).__name__}: {e}") from e
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


def take_outbox(gh: GitHub, tar: Path, branches: Mapping[str, str], now: float) -> bool:
    """Turn an ended VM's outbox into its repo's cache disk if the job that VM ran passed on
    a push to the default branch, so a branch or pull request cannot replace the cache. The
    header only says which run to ask about; GitHub's job list must name this VM's runner.
    Returns True to keep it for another try (GitHub does not show the job finished yet)."""
    runner = tar.stem
    try:
        fields = json.loads(tar.with_suffix(".json").read_text())
        repo = str(fields["repo"])
        with tar.open("rb") as f:
            first = f.read(512)
    except (OSError, ValueError, KeyError, TypeError):
        return False
    header = OUTBOX_HEADER.match(first)
    if not header:
        return False  # nothing saved: most VMs end so
    branch = branches.get(repo)
    if not branch:
        log(f"cache for {repo}: outbox of {runner} dropped, the repo is not served")
        return False
    run_id, attempt = int(header[1]), int(header[2])
    image = cache_image(repo)
    seen = image.with_suffix(".json")
    known = json.loads(seen.read_text()) if seen.exists() else {}
    if (run_id, attempt) <= (int(known.get("run", 0)), int(known.get("attempt", 0))):
        return False  # not newer than the run the disk came from
    path = f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}"
    run = gh.request("GET", path)
    jobs = gh.request("GET", f"{path}/jobs?per_page=100").get("jobs", [])
    job = next((j for j in jobs if j.get("runner_name") == runner), None)
    if not (job and run.get("event") == "push" and run.get("head_branch") == branch):
        log(f"cache for {repo}: outbox of {runner} is not from a push to {branch}; ignored")
        return False
    if job.get("status") != "completed":
        return now - tar.stat().st_mtime < OUTBOX_WAIT_SECONDS
    if job.get("conclusion") != "success":
        log(f"cache for {repo}: outbox of {runner} is from a job that did not pass; ignored")
        return False
    started = time.time()
    with tar.open("rb") as f:
        f.seek(512)
        try:
            build_cache_image(f, image)
        except BadCache as e:
            log(f"cache for {repo}: outbox of {runner} failed to unpack, {e}; ignored")
            return False
    seen.write_text(json.dumps({"run": run_id, "attempt": attempt, "sha": run.get("head_sha", "")}))
    log(f"cache for {repo}: built from run {run_id} in {time.time() - started:.0f}s")
    return False


def judge_outbox(gh: GitHub, tar: Path, branches: Mapping[str, str], now: float) -> bool:
    """take_outbox, deleting the outbox unless it is kept for another try. Only a failure that
    may clear (GitHub down or rate limited) keeps it: a VM could name a run that does not
    exist, and its 6 GiB would stay an hour."""
    try:
        keep = take_outbox(gh, tar, branches, now)
    except Exception as e:
        log(f"outbox {tar.name} failed: {type(e).__name__}: {e}")
        gone = isinstance(e, urllib.error.HTTPError) and e.code in (400, 404, 410, 422)
        keep = not gone and now - tar.stat().st_mtime < OUTBOX_WAIT_SECONDS
    if not keep:
        tar.unlink(missing_ok=True)
        tar.with_suffix(".json").unlink(missing_ok=True)
    return keep


def ended_outboxes(state: State, now: float) -> list[Path]:
    """The outboxes of VMs that have ended (each has its .json from launch), not tried in
    the last OUTBOX_RETRY_SECONDS."""
    running = {run.runner_name for run in state.launches.values()}
    return [
        record.with_suffix(".tar")
        for record in OUTBOX_DIR.glob(f"{RUNNER_PREFIX}*.json")
        if record.stem not in running
        and now - state.cache_checked.get(record.stem, 0) >= OUTBOX_RETRY_SECONDS
    ]


def maybe_take_outboxes(gh: GitHub, state: State, now: float) -> None:
    """Judge ended VMs' outboxes one at a time in the background, so polling never waits on
    a build. Each is deleted once judged; one GitHub cannot answer for is kept an hour."""
    due = ended_outboxes(state, now)
    if not due or not state.cache_busy.acquire(blocking=False):
        return
    branches = {r: p.default_branch for r, p in state.privacy.items() if p.private}
    for tar in due:
        state.cache_checked[tar.stem] = now
    # Its own client: the request cache is not shared between threads.
    own = gh.clone()

    def work() -> None:
        try:
            for tar in due:
                if not judge_outbox(own, tar, branches, now):
                    state.cache_checked.pop(tar.stem, None)
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
    # Why each queued job not started this poll waits: (repo, why).
    reasons: dict[int, tuple[str, str]] = {}
    # When GitHub queued each job, and its workflow.
    queued_at: dict[int, float] = {}
    workflows: dict[int, str] = {}
    # The labels of each job no VM has the labels of.
    unmatched: dict[int, tuple[str, ...]] = {}
    polled: list[Job] = []
    # All at once: one after another, 9 repos took 13 s a poll while a job waited (4 Nov).
    answers = state.askers.map(lambda repo: ask(gh, state, repo, now), repos)
    for repo, (jobs, took) in zip(repos, answers, strict=True):
        spent[repo] = took
        if jobs is None:
            continue
        private.append(repo)
        for job in jobs:
            queued_at[job.id] = job.queued_at
            workflows[job.id] = job.workflow
            if not job.ours:
                reasons[job.id] = (repo, f"no VM has its labels ({', '.join(job.labels)})")
                unmatched[job.id] = job.labels
                continue
            queued[job.want] += 1
            job_ids.setdefault(job.want, []).append(job.id)
            polled.append(job)
    reap(gh, state, now, queued, private)
    # Ended VMs' outboxes are after-the-fact too: a failure to list them never stops a start.
    try:
        maybe_take_outboxes(gh, state, now)
    except RateLimited:
        raise
    except Exception as e:
        log(f"could not look at ended VMs' outboxes: {type(e).__name__}: {e}")
    # A VM still on its way to a job, or a standby waiting for one, covers a queued job
    # with the same repo and cache label (or none).
    # Those whose runner is up and idle are ready: the rest of the wait is GitHub's.
    ways = [run for run in state.launches.values() if on_its_way(gh, run, now, queued[run.want])]
    booting: Counter[Want] = Counter(run.want for run in ways)
    ready: Counter[Want] = Counter(run.want for run in ways if run.ready)
    draining = DRAIN_FILE.exists()
    if state.draining and not draining:
        # A failed load test lowers the count while no VM runs; never raised here, as more
        # slots need install-host.sh's users and taps.
        state.slots = min(state.slots, slot_count())
    state.draining = draining
    free = [slot for slot in range(state.slots) if slot not in state.launches and not draining]
    # GitHub's job ids only grow, so the smallest is the oldest.
    order = start_order(polled, lambda i: waited_since(state, i, now), now)
    rank = {(job.id, job.want): n for n, job in enumerate(order)}
    in_order = [job.want for job in order]
    starts = plan(in_order, booting, len(free))
    # The VMs started this poll are counted by their size: they have touched almost
    # nothing yet, so MemAvailable does not show them.
    reserve = memory_reserve_mib()
    room = mem_available_mib() - reserve
    disk_wait = disk_wait_percent()
    disk_paced = disk_wait > DISK_WAIT_LIMIT / 2
    disk_busy = disk_wait > DISK_WAIT_LIMIT or (disk_paced and now < state.disk_paced_until)
    if disk_busy:
        room = min(room, 0)

    def why(need: int) -> str:
        """Why a job needing `need` MiB is held: the disk or memory."""
        if disk_busy or disk_paced:
            return f"the disk (tasks waited on it {disk_wait:.0f}% of the last minute)"
        return (
            f"memory ({max(room, 0)} MiB free above the {reserve} MiB reserve, a VM needs {need})"
        )

    def pace() -> None:
        """One VM a minute while the disk is half busy: no more room after this start."""
        nonlocal room
        if disk_paced:
            state.disk_paced_until = now + DISK_PACE_SECONDS
            room = min(room, 0)

    # In start order (a started run's job first, oldest first within each group, a new build
    # back in after FIRST_BUILD_WAIT_MAX); once one is held for memory, those after it wait too.
    builds = with_job_ids(starts, job_ids, booting, rank)
    held: dict[int, tuple[str, str]] = {}
    first_held_build: int | None = None
    started = 0
    for job_id, (repo, label) in sorted(builds, key=rank.__getitem__):
        mem = next_size(state, False)[1]
        if first_held_build is not None or mem > room:
            if first_held_build is None:
                first_held_build = job_id
            held[job_id] = (repo, why(mem))
            continue
        launch(
            gh,
            state,
            repo,
            free[started],
            now,
            label=label,
            cache=cache_for(repo, label, private),
            jobs=job_ids[(repo, label)],
        )
        room -= mem
        started += 1
        pace()
    launched = starts[:started]
    hold_for_memory(state, repos, held)
    # The rest of each want's jobs: the oldest are covered by VMs on their way or started
    # above, and the rest found no free slot.
    planned = {job_id for job_id, _ in builds}
    full = f"all {state.slots} build slots in use"
    for want, ids in job_ids.items():
        for n, job_id in enumerate(sorted(ids)):
            if draining:
                reasons[job_id] = (want[0], "the host's load test")
            elif job_id in held:
                reasons[job_id] = held[job_id]
            elif n < ready[want]:
                reasons[job_id] = (want[0], READY)
            elif n < booting[want] or job_id in planned:
                reasons[job_id] = (want[0], BOOTING)
            else:
                reasons[job_id] = (want[0], full)
    note_waits(state, private, reasons, now, queued_at, workflows, unmatched)
    free = free[started:]
    # A build job still waiting for a slot, or for memory, gets the standby's slot and
    # memory, if the standby cannot take it.
    waiting = queued - booting
    waiting.subtract(launched)
    for run in list(state.launches.values()):
        if run.standby and any(n > 0 and want != run.want for want, n in waiting.items()):
            stop_if_still_idle(gh, run, "is in the way of a queued job")
    if draining:
        for run in [r for r in state.launches.values() if r.standby]:
            stop_if_still_idle(gh, run, "gives way to the host's load test")
        return spent
    standbys = [r for r in state.launches.values() if r.standby]
    # A standby is the first thing given up: never booted while a job waits for memory, nor
    # while a build job waits for a slot (one at a time while free slots outnumber them).
    waiting_builds = sum(n for n in waiting.values() if n > 0)
    # Each want gets one first; once all have one, a further standby waits for the most needed,
    # so two tasks of the busiest repo starting builds together both find one booted.
    wants = standby_wants(state, repos, private)
    uncovered = next((w for w in wants if all(r.want != w for r in standbys)), None)
    target = uncovered or (wants[0] if wants else None)
    if uncovered and len(standbys) >= standby_count():
        # A second standby for one want gives its place to a want with none.
        seen: set[Want] = set()
        for run in sorted(standbys, key=lambda r: r.started):
            if run.want in seen:
                stop_if_still_idle(gh, run, "gives its place to a standby for another job")
                break
            seen.add(run.want)
    if (
        target
        and not state.held
        and len(free) > waiting_builds
        and len(standbys) < standby_count()
        and next_size(state, True)[1] <= room
    ):
        repo, label = target
        cache = cache_for(repo, label, private)
        launch(gh, state, repo, free[0], now, label=label, cache=cache, standby=True)
        room -= next_size(state, True)[1]
        pace()
    return spent


def with_job_ids(
    starts: list[Want],
    job_ids: Mapping[Want, list[int]],
    covered: Mapping[Want, int],
    rank: Mapping[tuple[int, Want], int],
) -> list[tuple[int, Want]]:
    """Each planned start with the id of the job it is for: a want's first jobs in start
    order not covered by a VM already on its way, so a task's review sharing a want with an
    older goal owner gets memory at the review's place."""
    taken = Counter(covered)
    paired: list[tuple[int, Want]] = []
    for want in starts:
        ids = sorted(job_ids[want], key=lambda i: rank[(i, want)])
        paired.append((ids[taken[want]], want))
        taken[want] += 1
    return paired


def hold_for_memory(state: State, repos: list[str], held: Mapping[int, tuple[str, str]]) -> None:
    """Note the queued jobs left waiting for memory or the disk (job id: its repo and why).
    They stay queued and are tried again next poll; note_waits logs them. Jobs of repos not
    polled this time keep their mark."""
    state.held = {i: r for i, r in state.held.items() if r not in repos} | {
        i: repo for i, (repo, _) in held.items()
    }


def duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m{seconds % 60:02d}s"


def waited_since(state: State, job_id: int, now: float) -> float:
    """When the supervisor first saw the job waiting (now, if it never did)."""
    wait = state.waits.get(job_id)
    return wait.since if wait else now


def note_waits(
    state: State,
    repos: list[str],
    reasons: Mapping[int, tuple[str, str]],
    now: float,
    queued_at: Mapping[int, float] | None = None,
    workflows: Mapping[int, str] | None = None,
    unmatched: Mapping[int, tuple[str, ...]] | None = None,
) -> None:
    """Log each queued job not started, with why and how long it has waited here: when it
    starts waiting and every WAIT_LOG_SECONDS, and at once if GitHub had queued it
    UNSEEN_LOG_SECONDS before the supervisor saw it. A job that was logged gets one more line
    when it stops waiting. Jobs of repos not polled this time are left as they are."""
    queued_at = queued_at or {}
    workflows = workflows or {}
    unmatched = unmatched or {}
    for job_id, wait in list(state.waits.items()):
        if job_id not in reasons and wait.repo in repos:
            del state.waits[job_id]
            wait.count(now)
            record_wait(state, job_id, wait, now)
            if wait.logged is not None:
                waited = duration(now - wait.since)
                total = f", {duration(now - wait.queued)} since GitHub queued it"
                log(
                    f"job {job_id} in {wait.repo} stopped waiting after {waited}"
                    + (total if wait.queued < wait.since else "")
                )
    for job_id, (repo, why) in sorted(reasons.items()):
        wait = state.waits.get(job_id)
        unseen = 0.0
        if wait is None:
            queued = min(queued_at.get(job_id) or now, now)
            wait = state.waits[job_id] = Wait(
                repo, now, queued, seen=now, workflow=workflows.get(job_id, "")
            )
            unseen = wait.seconds["unseen"] = now - queued
        wait.count(now)
        wait.kind = wait_kind(why)
        wait.why = wait.whys[wait.kind] = why
        wait.unmatched = unmatched.get(job_id, ())
        waited = now - wait.since
        late = unseen >= UNSEEN_LOG_SECONDS
        quiet = why in (BOOTING, READY) and waited < WAIT_LOG_SECONDS and not late
        if not quiet and (wait.logged is None or now - wait.logged >= WAIT_LOG_SECONDS):
            seen = f" (GitHub had queued it {duration(unseen)} before)" if late else ""
            log(f"job {job_id} in {repo} has waited {duration(waited)} here{seen}: {why}")
            wait.logged = now
    if now - state.wait_metrics_written >= WAIT_METRICS_SECONDS:
        write_wait_metrics(state, now)


def write_wait_metrics(state: State, now: float) -> None:
    """Write the longest-waiting queued job's seconds per kind (all 0 when none waits), with
    the repo and labels of the longest-waiting job no VM has the labels of on the "other"
    kind (it never starts here, so the alert names it), the seconds since GitHub queued each
    workflow's longest-waiting job (none when nothing waits), and how many VMs run a job of
    how many slots, so an alert can tell a job held for a VM from one GitHub never queued.
    A failure is logged once and never stops the poll."""
    longest = min(state.waits.values(), key=lambda w: w.queued, default=None)
    seconds = longest.seconds if longest else {}
    stray = min(
        (w for w in state.waits.values() if w.unmatched), key=lambda w: w.queued, default=None
    )
    names = {kind: f'kind="{kind}"' for kind in WAIT_KINDS}
    if stray:
        labels = label_value(",".join(stray.unmatched))
        names["other"] += f',repo="{label_value(stray.repo)}",labels="{labels}"'
    lines = [
        f"homelab_ci_wait_seconds{{{names[kind]}}} {round(seconds.get(kind, 0.0))}"
        for kind in WAIT_KINDS
    ]
    queued: dict[tuple[str, str], float] = {}
    for wait in state.waits.values():
        key = (wait.repo, wait.workflow)
        queued[key] = max(queued.get(key, 0.0), now - wait.queued)
    lines += [
        f'homelab_ci_queued_seconds{{repo="{label_value(repo)}",workflow="{label_value(name)}"}}'
        f" {round(waited)}"
        for (repo, name), waited in sorted(queued.items())
    ]
    busy = sum(not run.standby for run in state.launches.values())
    failed_for = now - state.poll_failed_since if state.poll_failed_since is not None else 0.0
    lines += [
        f"homelab_ci_vms_busy {busy}",
        f"homelab_ci_vm_slots {state.slots}",
        f"homelab_ci_poll_failures {state.poll_failures}",
        f"homelab_ci_poll_failed_seconds {round(failed_for)}",
    ]
    try:
        write_textfile(WAIT_METRICS, lines)
        state.wait_metrics_written = now
        state.wait_metrics_error = ""
    except OSError as e:
        why = f"{type(e).__name__}: {e}"
        if why != state.wait_metrics_error:
            log(f"could not write {WAIT_METRICS}: {why}")
        state.wait_metrics_error = why


def note_poll(state: State, ok: bool, now: float) -> None:
    """Counts failed polls in a row, for OpenObserve: no VM starts while every poll fails. The
    metrics are written at once when polls start or stop
    failing, and otherwise at most every WAIT_METRICS_SECONDS."""
    was_failing = state.poll_failures > 0
    if ok:
        state.poll_failures, state.poll_failed_since = 0, None
    else:
        state.poll_failures += 1
        if state.poll_failed_since is None:
            state.poll_failed_since = now
    changed = was_failing != (state.poll_failures > 0)
    if changed or now - state.wait_metrics_written >= WAIT_METRICS_SECONDS:
        write_wait_metrics(state, now)


def label_value(text: str) -> str:
    """Text as a Prometheus label value."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def wait_kind(why: str) -> str:
    """Which of WAIT_KINDS a reason from tick is. A job queued behind one held for memory
    waits for memory too."""
    if why == BOOTING:
        return "booting"
    if why == READY:
        return "ready"
    if why.startswith("the disk"):
        return "disk"
    if why.startswith("memory") or why.endswith("waiting for memory"):
        return "memory"
    if "slots in use" in why:
        return "slots"
    return "other"


def record_wait(state: State, job_id: int, wait: Wait, now: float) -> None:
    """Append the job's wait to WAITS_FILE, pruning old lines first when due. A failure is
    logged once and never stops the poll."""
    line = {"job": job_id, "repo": wait.repo, "since": int(wait.since), "until": int(now)}
    line |= {kind: round(wait.seconds.get(kind, 0.0)) for kind in WAIT_KINDS}
    line["why"] = wait.longest_why()
    try:
        if now - state.waits_pruned >= WAITS_PRUNE_SECONDS:
            prune_waits(now)
            state.waits_pruned = now
        with WAITS_FILE.open("a") as f:
            f.write(json.dumps(line) + "\n")
        WAITS_FILE.chmod(0o644)
        state.waits_error = ""
    except OSError as e:
        why = f"{type(e).__name__}: {e}"
        if why != state.waits_error:
            log(f"could not write {WAITS_FILE}: {why}")
        state.waits_error = why


def prune_waits(now: float) -> None:
    """Drop the lines of waits that ended over WAITS_KEEP_SECONDS ago."""
    try:
        lines = WAITS_FILE.read_text().splitlines()
    except FileNotFoundError:
        return
    kept: list[str] = []
    for text in lines:
        try:
            if now - float(json.loads(text)["until"]) < WAITS_KEEP_SECONDS:
                kept.append(text + "\n")
        except (ValueError, KeyError, TypeError):
            continue  # a half-written line from a crash
    replace_file(WAITS_FILE, "".join(kept))


def standby_wants(state: State, repos: list[str], private: list[str]) -> list[Want]:
    """The repos and cache labels build standbys boot for, most needed first: the last build
    job's (or the first polled repo's) once no VM runs it, then each running build job's,
    oldest first (a task's Reviewer asks for its Builder's seconds after the Builder ends, even
    when another task's build started in between). Booted for the cache label too, else a job
    asking for it never takes the standby and stops it as in the way. Each gets one standby
    first; any left under standby_count() go to the first, and give way to a want with none."""
    running = sorted(
        (r for r in state.launches.values() if not r.standby),
        key=lambda r: r.started,
    )
    wants = [r.want for r in running]
    if repos:
        last = (repos[0], state.last_label if repos[0] == state.last_repo else "")
        # The last build job's want leads once its VM has ended: that is when it is asked for.
        wants = [*wants, last] if last in wants else [last, *wants]
    return [w for w in dict.fromkeys(wants) if w[0] in private]


def standby_count() -> int:
    """How many build standbys may wait (see standby_wants for which): 2 unless
    /etc/homelab-ci/standbys says otherwise."""
    return count_in(STANDBY_COUNT_FILE, default=2)


def count_in(path: Path, default: int = 1) -> int:
    """How many standbys a host file asks for: `default` if it is missing or not a number."""
    try:
        return max(0, int(path.read_text().strip()))
    except (OSError, ValueError):
        return default


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
    runs = state.launches.values()
    standby = sum(run.standby for run in runs)
    return {
        "updated": int(now),
        "slots": state.slots,
        "in_use": len(state.launches) - standby,
        "standby": standby,
        "cache_disks": sorted(
            image.stem.replace("__", "/", 1) for image in CACHE_DIR.glob("*__*.ext4")
        ),
        "github_ok": github_ok,
        "mem_available_mib": mem_available,
        "held_for_memory": len(state.held),
        "queued": len(state.waits),
        "in_use_repos": dict(Counter(run.repo for run in runs if not run.standby)),
        "draining": state.draining,
        "load_test": read_load_test(),
    }


def read_load_test() -> dict[str, object] | None:
    """The last load test's record, or None if there is none or it is not a JSON object."""
    try:
        record = json.loads(LOAD_TEST_FILE.read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_test_due(record: Mapping[str, object] | None, slots: int, test: str, now: float) -> bool:
    """Whether to run the load test: never run at this slot count and test version, or it did
    not run then (the record has no result) and LOAD_TEST_RETRY_SECONDS have passed. A failed
    test is not run again: it lowered the count, and the lower count is due."""
    if record is None or record.get("slots") != slots or record.get("test") != test:
        return True
    at = record.get("at")
    return not record.get("ran") and (
        not isinstance(at, int | float) or now - at >= LOAD_TEST_RETRY_SECONDS
    )


def start_load_test_if_due() -> int:
    """`start-load-test-if-due`, from update.sh: start LOAD_TEST_UNIT when the test is due."""
    try:
        test = file_hash(LOAD_TEST_SCRIPT)
    except OSError as e:
        log(f"no load test to run: {type(e).__name__}: {e}")
        return 0
    if load_test_due(read_load_test(), slot_count(), test, time.time()):
        log(f"starting {LOAD_TEST_UNIT}: no result for {slot_count()} slots and this test")
        # Refused once the unit's start limit is reached: it is started again after that.
        if subprocess.run(
            ["systemctl", "start", "--no-block", LOAD_TEST_UNIT], timeout=30
        ).returncode:
            log(f"systemd did not start {LOAD_TEST_UNIT}; tried again on the next pass")
    return 0


def run_load_test() -> int:
    """`load-test`, LOAD_TEST_UNIT's work: drain, run test-load.sh at every slot, keep the
    result, and on a FAIL lower the slot count by one for the next run to test. Whatever
    breaks after the drain starts is kept as "not run", so the test waits
    LOAD_TEST_RETRY_SECONDS instead of draining the host again on every update pass."""
    slots, test = slot_count(), file_hash(LOAD_TEST_SCRIPT)
    commit = read_word(COMMIT_FILE) or ""
    kept = False

    def keep(ran: bool, passed: bool, line: str) -> None:
        nonlocal kept
        record = {"slots": slots, "passed": passed, "ran": ran, "line": line}
        record |= {"at": int(time.time()), "commit": commit, "test": test}
        replace_file(LOAD_TEST_FILE, json.dumps(record))
        kept = True
        log(f"load test at {slots} slots: {line}")

    DRAIN_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    DRAIN_FILE.touch()
    try:
        return drain_and_test(slots, keep)
    except Exception as e:
        if not kept:
            keep(False, False, f"not run: {type(e).__name__}: {e}")
        raise
    finally:
        DRAIN_FILE.unlink(missing_ok=True)


def drain_and_test(slots: int, keep: Callable[[bool, bool, str], None]) -> int:
    """Wait for the running VMs to end, then run test-load.sh and keep its result."""
    deadline = time.monotonic() + DRAIN_LIMIT_SECONDS
    log("draining: no new VMs; waiting for the running ones to end")
    # None (systemd did not answer) is not known to be empty: wait on.
    while (vms := running_vms()) != set():
        if time.monotonic() >= deadline:
            count = "some" if vms is None else str(len(vms))
            keep(False, False, f"not run: {count} VMs still running after the drain limit")
            return 1
        time.sleep(DRAIN_LOOK_SECONDS)
    line = ""
    with UPDATE_LOCK.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with subprocess.Popen(
            [str(LOAD_TEST_SCRIPT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "SLOTS": str(slots)},
        ) as proc:
            assert proc.stdout is not None
            for out in proc.stdout:
                print(out, end="", flush=True)
                if out.startswith(("PASS ", "FAIL ")):
                    line = out.strip()
        code = proc.returncode
    passed = code == 0 and line.startswith("PASS ")
    if not line:
        keep(False, False, f"not run: test-load.sh exited {code} with no PASS or FAIL line")
        return 1
    keep(True, passed, line)
    if not passed and slots > MIN_TESTED_SLOTS:
        SLOTS_FILE.write_text(f"{slots - 1}\n")
        log(f"lowered the slot count to {slots - 1}; the next update tests it")
    return 0 if passed else 1


def write_status(path: Path, data: Mapping[str, Any]) -> None:
    replace_file(path, json.dumps(data))


def replace_file(path: Path, text: str) -> None:
    """Replace the file whole, readable by anyone, so a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o755)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def served_repos(gh: GitHub) -> dict[str, str]:
    """Colin's private repositories the token can see, the ones that signed up, each
    with its default branch. An archived one takes no jobs, so it gets no VM or standby."""
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
        if r.get("private") is True
        and not r.get("archived")
        and (r.get("owner") or {}).get("login") == OWNER
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


# The commit install-host.sh last installed (ci-runner/update.sh writes it).
COMMIT_FILE = Path("/etc/homelab-ci/commit")


def read_word(path: Path) -> str:
    try:
        return path.read_text().split()[0]
    except (OSError, IndexError):
        return ""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["vm"]:
        return vm_main(argv[1:])
    if argv == ["start-load-test-if-due"]:
        return start_load_test_if_due()
    if argv == ["load-test"]:
        return run_load_test()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="one poll, for testing")
    args = parser.parse_args(argv)
    gh = GitHub(TOKEN_FILE.read_text().strip())
    repos: list[str] = []
    listed = 0.0
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    CACHE_DIR.mkdir(mode=0o755, exist_ok=True)
    OUTBOX_DIR.mkdir(mode=0o700, exist_ok=True)
    # Half-built caches from a supervisor that was killed mid-build.
    for leftover in CACHE_DIR.glob("tmp*"):
        subprocess.run(["rm", "-rf", "--one-file-system", str(leftover)], check=False)
    state = State(slots=slot_count())
    write_memory_metrics()
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
            github_ok = True
            note_poll(state, True, time.time())
        except RateLimited as e:
            log(str(e))
            wait = max(POLL_SECONDS, e.until - time.time() + 1)
        except Exception as e:
            log(f"poll failed: {type(e).__name__}: {e}")
            note_poll(state, False, time.time())
            # Without GitHub nothing re-checks privacy, so no VM is left waiting meanwhile.
            if state.poll_failures == 5:
                for run in [r for r in state.launches.values() if r.standby]:
                    stop(run, "cannot be checked while GitHub fails")
        # Written after a failed poll too, so it still shows the supervisor is alive.
        try:
            write_status(STATUS_FILE, status(state, time.time(), github_ok, mem_available_mib()))
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
