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
import http.client
import io
import json
import os
import queue
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
# Light jobs that run no agent ask for this instead and get half the memory: they peak
# under 700 MiB, so more of them fit beside the builds.
SMALL_LABEL = "homelab-fc-small"
LIGHT_SLOTS_FILE = Path("/etc/homelab-ci/light-slots")
# None until install-host.sh writes the file, as it also makes their users and taps.
DEFAULT_LIGHT_SLOTS = 0
# vCPUs and MiB by label. One core, as they mostly wait; 2 GiB for jobs running Claude
# Code with no swap.
LIGHT_SIZES = {LIGHT_LABEL: (1, 2048), SMALL_LABEL: (1, 1024)}


def light_runner_labels(label: str) -> tuple[str, ...]:
    return ("self-hosted", "linux", "x64", label)


# A merge job waits for a pull request's CI, two rounds at most.
LIGHT_TIMEOUT_SECONDS = 3 * 3600
# How many light VMs wait ready for the last light job's repo, so a task's first and last
# steps start in seconds. /etc/homelab-ci/light-standbys changes it with no code change.
LIGHT_STANDBY_COUNT_FILE = Path("/etc/homelab-ci/light-standbys")
LAST_LIGHT_REPO_FILE = Path("/var/lib/homelab-ci/last-light-repo")
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
# Each repo's read-only cache disk: its package caches and installs, built from what a
# green push to its default branch leaves on its VM's outbox disk (see ci-runner.md).
CACHE_DIR = Path("/var/lib/homelab-ci/caches")
# / is shared with everything. run-vm.sh sizes the outbox to this and its header.
CACHE_MAX_UNPACKED_BYTES = 6 * 1024**3
# Each VM's outbox (<runner name>.tar) and the repo it ran for (<runner name>.json), kept
# after the VM until judged, and its inbox (<runner name>.inbox). Not through GitHub
# artifacts: they count against the account's storage quota, and once it is full every
# upload fails.
OUTBOX_DIR = Path("/var/lib/homelab-ci/outbox")
# What the fc-cache action writes last, once the tar after it is complete.
OUTBOX_HEADER = re.compile(rb"homelab-ci-cache v1 run=([0-9]{1,20}) attempt=([0-9]{1,5})\n")
# An outbox whose job GitHub does not show finished yet is tried again a minute later,
# for up to an hour; one with handoffs a few seconds later, since the next job waits on it.
OUTBOX_RETRY_SECONDS = 60
HANDOFF_RETRY_SECONDS = 5
OUTBOX_WAIT_SECONDS = 3600
# Files a job hands to later jobs (the handoff action, see ci-runner.md):
# <owner>__<name>/<run>/<job>/<item>.tar and .json, filed under the job name GitHub reports
# for the VM that wrote them, so no job can write as another.
HANDOFF_DIR = Path("/var/lib/homelab-ci/handoff")
# What the handoff action writes last on the outbox: its tar holds <item>.tar and <item>.json.
HANDOFF_HEADER = re.compile(rb"homelab-ci-handoff v1 run=([0-9]{1,20}) attempt=([0-9]{1,5})\n")
HANDOFF_ITEM = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
HANDOFF_JOB = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}")
HANDOFF_MAX_DAYS = 90
# All of one repo's handoffs: past this, new ones are dropped (the disk is shared).
HANDOFF_REPO_MAX_BYTES = 20 * 1024**3
HANDOFF_SWEEP_SECONDS = 3600
# A job asks for a handoff with a console line; its VM's console reader writes the answer
# on the VM's read-only inbox disk: the item's tar after 512 bytes, then the header.
HANDOFF_ASK = re.compile(
    rb"HOMELAB-HANDOFF 1 ([a-z0-9]{8,32}) ([0-9]{1,20}) ([0-9]{1,5}) ([0-9]{1,20}) "
    rb"(\S{1,100}) (\S{1,64}) ([0-9]{1,4})\r?\n?"
)
HANDOFF_MAX_WAIT_SECONDS = 1800
# Asks one VM may make, and wait in line: a job makes a few.
HANDOFF_MAX_ASKS = 50
# How often a waiting ask looks for the item, and asks GitHub whether it can still come.
HANDOFF_LOOK_SECONDS = 2
HANDOFF_STATE_SECONDS = 15
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
# A queued job not started is logged with why (slots, memory, disk, labels) when it starts
# waiting and then every this many seconds, to explain a long wait.
WAIT_LOG_SECONDS = 180.0
# A job a VM is booting for (or a standby waits ready for) is logged only once it has
# waited WAIT_LOG_SECONDS, so every job's few seconds of boot are not logged.
BOOTING = "a VM booting or waiting ready for it"


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

    @property
    def want(self) -> Want:
        light = LIGHT_SIZES.keys() & {label.lower() for label in self.labels}
        if light:
            return (self.repo, min(light))
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
        return self.label in LIGHT_SIZES


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
    handoff_busy: threading.Lock = field(default_factory=threading.Lock)
    # The threads that ask the polled repos for queued jobs at once, kept so each keeps
    # its open connection to GitHub.
    askers: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(ASK_THREADS, thread_name_prefix="ask")
    )
    # The repository and cache label of the last build job a VM started for: the standby
    # boots for the same, so the next such job (a task's Builder, then its Reviewer) takes it.
    last_repo: str = ""
    last_label: str = ""
    # The repository and label of the last light job: the light standby boots for them.
    last_light_repo: str = ""
    last_light_label: str = LIGHT_LABEL
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


@dataclass
class Wait:
    repo: str
    since: float
    logged: float | None = None


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
    light = LIGHT_SIZES.keys() & asked
    if light:
        return len(light) == 1 and asked <= set(light_runner_labels(*light))
    extra = asked - set(RUNNER_LABELS)
    return LABEL in asked and len(extra) <= 1 and all(map(cache_label_repo, extra))


def queued_jobs(
    repo: str, runs: Iterable[Mapping[str, Any]], get_jobs: Callable[[int], Any]
) -> list[Job]:
    jobs: list[Job] = []
    for run in runs:
        for job in get_jobs(int(run["id"])):
            labels = tuple(str(label) for label in job.get("labels", []))
            if job.get("status") != "queued":
                continue
            ours = wants_us(labels)
            # One asking for our label with another we lack is kept, to log why it waits.
            if ours or {LABEL, *LIGHT_SIZES} & {label.lower() for label in labels}:
                jobs.append(Job(repo, int(job["id"]), str(job.get("name", "")), labels, ours))
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
    light = label in LIGHT_SIZES
    # The cache label is registered too, so a job asking for it runs only on such a VM.
    if light:
        labels = list(light_runner_labels(label))
    else:
        labels = [*RUNNER_LABELS, label] if label else list(RUNNER_LABELS)
    registration = gh.request(
        "POST",
        f"/repos/{repo}/actions/runners/generate-jitconfig",
        {"name": name, "runner_group_id": 1, "labels": labels, "work_folder": "_work"},
    )
    runner_id = int(registration["runner"]["id"])
    metadata = STATE_DIR / f"metadata-slot{slot}.json"
    vcpus, mem_mib = LIGHT_SIZES[label] if light else next_size(state, standby)
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
        (OUTBOX_DIR / f"{name}.json").write_text(json.dumps({"repo": repo, "light": light}))
        env["CACHE_OUT"] = str(OUTBOX_DIR / f"{name}.tar")
        env["INBOX"] = str(OUTBOX_DIR / f"{name}.inbox")
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
    if light and not standby and (repo, label) != (state.last_light_repo, state.last_light_label):
        state.last_light_repo, state.last_light_label = repo, label
        remember(LAST_LIGHT_REPO_FILE, f"{repo}\n{label}\n")
    kind = "standby" if standby else "VM"
    disk = cache.name if cache else "no cache disk"
    log(f"slot {slot}: started {kind} {name} for {repo}, {disk} ({vcpus} vCPUs, {mem_mib} MiB)")


def remember_last_job(repo: str, label: str) -> None:
    remember(LAST_REPO_FILE, f"{repo}\n{label}\n")


def remember(path: Path, text: str) -> None:
    """Kept for the next start; losing it only sends the first standby elsewhere."""
    try:
        path.write_text(text)
    except OSError as e:
        log(f"could not keep the last job's repo: {type(e).__name__}: {e}")


def recalled_last_light_job() -> Want:
    """The last light job's repo and label (the 2 GiB one from an older file)."""
    try:
        lines = LAST_LIGHT_REPO_FILE.read_text().split()
    except OSError:
        return ("", LIGHT_LABEL)
    label = lines[1] if len(lines) > 1 and lines[1] in LIGHT_SIZES else LIGHT_LABEL
    return (lines[0] if lines else "", label)


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
    vm += ["--repo", run.repo, "--runner", run.runner_name]
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
    """`vm SLOT METADATA TIMEOUT [--live FILE --session FILE] [--repo R --runner N]`: run-vm.sh's
    console reader, the main process of the VM's unit. It reads the console to the end (a full
    pipe would stall the VM), keeps the latest live line in FILE and the session events, hands
    handoff asks to a worker that answers them on the VM's inbox, and exits with run-vm.sh's
    code."""
    parser = argparse.ArgumentParser(prog="ci_supervisor.py vm")
    parser.add_argument("slot")
    parser.add_argument("metadata")
    parser.add_argument("timeout")
    parser.add_argument("--live", type=Path)
    parser.add_argument("--session", type=Path)
    # Whose VM it is, to answer its handoff asks.
    parser.add_argument("--repo")
    parser.add_argument("--runner")
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
    asks: queue.Queue[re.Match[bytes] | None] = queue.Queue(HANDOFF_MAX_ASKS)

    def hand_on(ask: re.Match[bytes]) -> None:
        with contextlib.suppress(queue.Full):  # the reader must never wait
            asks.put_nowait(ask)

    inbox = OUTBOX_DIR / f"{args.runner}.inbox"
    if args.repo and args.runner:
        gh = GitHub(TOKEN_FILE.read_text().strip())
        threading.Thread(
            target=answer_asks, args=(gh, args.repo, args.runner, inbox, asks), daemon=True
        ).start()
    try:
        read_console(process.stdout, keep, args.session, hand_on if args.runner else None)
        return process.wait()
    finally:
        with contextlib.suppress(queue.Full):
            asks.put_nowait(None)
        inbox.unlink(missing_ok=True)


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
    stream: IO[bytes],
    keep: Callable[[dict[str, Any]], None] | None,
    session: Path | None = None,
    ask: Callable[[re.Match[bytes]], None] | None = None,
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
            if ask and fits and (asked := HANDOFF_ASK.fullmatch(chunk)):
                ask(asked)
            elif keep and fits and (line := live_line(chunk, time.time())):
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
                    write_status(
                        run.record, record(run)
                    )  # a restart must not take it for a standby
                    if not run.light:
                        state.used.add(run.repo)
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
    """File an ended VM's handoffs (take_handoffs), or turn its outbox into its repo's cache
    disk if the job that VM ran passed on
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
    if handoff := HANDOFF_HEADER.match(first):
        return take_handoffs(gh, tar, repo, int(handoff[1]), int(handoff[2]), now)
    header = OUTBOX_HEADER.match(first)
    if not header:
        return False  # nothing saved: most VMs end so
    if fields.get("light"):
        log(f"cache for {repo}: outbox of light VM {runner} ignored, only build VMs make caches")
        return False
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


def holds_handoffs(tar: Path) -> bool:
    try:
        with tar.open("rb") as f:
            return bool(HANDOFF_HEADER.match(f.read(512)))
    except OSError:
        return False


def ended_outboxes(state: State, now: float) -> tuple[list[Path], list[Path]]:
    """The outboxes of VMs that have ended (each has its .json from launch), not tried in
    the last OUTBOX_RETRY_SECONDS (HANDOFF_RETRY_SECONDS for handoffs): (handoffs, rest)."""
    running = {run.runner_name for run in state.launches.values()}
    handoffs: list[Path] = []
    rest: list[Path] = []
    for inbox in OUTBOX_DIR.glob(f"{RUNNER_PREFIX}*.inbox"):
        if inbox.stem not in running:
            inbox.unlink(missing_ok=True)
    for record in OUTBOX_DIR.glob(f"{RUNNER_PREFIX}*.json"):
        if record.stem in running:
            continue
        tar = record.with_suffix(".tar")
        handoff = holds_handoffs(tar)
        wait = HANDOFF_RETRY_SECONDS if handoff else OUTBOX_RETRY_SECONDS
        if now - state.cache_checked.get(record.stem, 0) >= wait:
            (handoffs if handoff else rest).append(tar)
    return handoffs, rest


def maybe_take_outboxes(gh: GitHub, state: State, now: float) -> None:
    """Judge ended VMs' outboxes one at a time in the background, so polling never waits on
    a build; handoffs in a lane of their own, so the next job never waits behind a cache
    build. Each is deleted once judged; one GitHub cannot answer for is kept an hour."""
    handoffs, rest = ended_outboxes(state, now)
    branches = {r: p.default_branch for r, p in state.privacy.items() if p.private}
    for due, busy in ((handoffs, state.handoff_busy), (rest, state.cache_busy)):
        if not due or not busy.acquire(blocking=False):
            continue
        for tar in due:
            state.cache_checked[tar.stem] = now
        # Its own client: the request cache is not shared between threads.
        own = gh.clone()

        def work(due: list[Path] = due, busy: threading.Lock = busy, own: GitHub = own) -> None:
            try:
                for tar in due:
                    if not judge_outbox(own, tar, branches, now):
                        state.cache_checked.pop(tar.stem, None)
            finally:
                busy.release()

        threading.Thread(target=work, daemon=True).start()


def repo_handoffs(repo: str) -> Path:
    return HANDOFF_DIR / repo.replace("/", "__")


def take_handoffs(gh: GitHub, tar: Path, repo: str, run_id: int, attempt: int, now: float) -> bool:
    """File an ended VM's handoffs under the job GitHub shows that VM ran, whatever its
    result (a failed Builder's session is what its next try resumes). The header only says
    which run to ask about. Returns True to keep the outbox for another try."""
    runner = tar.stem
    path = f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"
    jobs = gh.request("GET", path).get("jobs", [])
    job = next((j for j in jobs if j.get("runner_name") == runner), None)
    if not job:
        log(f"handoff for {repo}: outbox of {runner} names run {run_id}, not its own; ignored")
        return False
    if job.get("status") != "completed":
        return now - tar.stat().st_mtime < OUTBOX_WAIT_SECONDS
    name = str(job.get("name", ""))
    if not HANDOFF_JOB.fullmatch(name):
        log(f"handoff for {repo}: job name {name!r} cannot be a folder; ignored")
        return False
    root = repo_handoffs(repo)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder = root / str(run_id) / name
    used = sum(p.stat().st_size for p in root.glob("*/*/*.tar"))
    staging = Path(tempfile.mkdtemp(prefix=f".{runner}-", dir=root))
    try:
        with tar.open("rb") as f:
            f.seek(512)
            items = unpack_handoffs(f, staging, HANDOFF_REPO_MAX_BYTES - used)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        for item, wants in items.items():
            meta = folder / f"{item}.json"
            if meta.exists() and int(json.loads(meta.read_text()).get("attempt", 0)) > attempt:
                continue  # a later attempt's already here
            (staging / f"{item}.tar").replace(folder / f"{item}.tar")
            fields = {
                "attempt": attempt,
                "conclusion": job.get("conclusion"),
                "saved": int(now),
                "expires": int(now + wants["days"] * 86400),
                "readers": wants["readers"],
                "later": wants["later"],
            }
            staged = staging / f"{item}.meta"
            staged.write_text(json.dumps(fields))
            staged.replace(meta)
        log(f"handoff for {repo}: run {run_id} {name} saved {', '.join(items) or 'nothing'}")
    except BadCache as e:
        log(f"handoff for {repo}: outbox of {runner} dropped, {e}")
    finally:
        subprocess.run(["rm", "-rf", "--one-file-system", str(staging)], check=False)
    return False


def unpack_handoffs(stream: IO[bytes], staging: Path, most: int) -> dict[str, dict[str, Any]]:
    """The outbox's <item>.tar and <item>.json files, into staging, at most `most` bytes in
    all; returns each item's readers, later readers and days. Names, sizes and the JSON are
    the VM's: checked, never trusted (a sparse member would unpack to any size it claims)."""
    wants: dict[str, dict[str, Any]] = {}
    found: set[str] = set()
    written = 0
    try:
        with tarfile.open(fileobj=stream, mode="r|") as tar:
            for member in tar:
                name = member.name.removeprefix("./")
                item, _, kind = name.rpartition(".")
                ours = HANDOFF_ITEM.fullmatch(item) and kind in ("tar", "json")
                if not (member.isfile() and not member.issparse() and ours):
                    continue
                source = tar.extractfile(member)
                if source is None:
                    continue
                if kind == "json":
                    wants[item] = handoff_wants(source.read(65536))
                    continue
                with (staging / name).open("wb") as out:
                    while chunk := source.read(1024**2):
                        written += len(chunk)
                        if written > most:
                            raise BadCache("over the repo's handoff limit")
                        out.write(chunk)
                found.add(item)
    except BadCache:
        raise
    except Exception as e:  # anything the VM's bytes can raise, deep JSON included
        raise BadCache(f"unreadable, {type(e).__name__}: {e}") from e
    return {item: wants[item] for item in sorted(found & wants.keys())}


def handoff_wants(raw: bytes) -> dict[str, Any]:
    fields = json.loads(raw)
    names = [
        [str(n) for n in fields.get(key, []) if HANDOFF_JOB.fullmatch(str(n))][:20]
        for key in ("readers", "later")
    ]
    days = min(max(int(fields.get("days", HANDOFF_MAX_DAYS)), 1), HANDOFF_MAX_DAYS)
    return {"readers": names[0], "later": names[1], "days": days}


def sweep_handoffs(now: float) -> None:
    """Delete handoffs past their days, and staging left by a supervisor that was killed."""
    for meta in HANDOFF_DIR.glob("*/*/*/*.json"):
        try:
            if json.loads(meta.read_text()).get("expires", 0) < now:
                meta.with_suffix(".tar").unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
        except (OSError, ValueError, AttributeError):
            continue
    for folder in sorted(HANDOFF_DIR.glob("*/*/*"), reverse=True) + sorted(HANDOFF_DIR.glob("*/*")):
        if folder.name.startswith("."):
            if now - folder.stat().st_mtime > OUTBOX_WAIT_SECONDS:
                subprocess.run(["rm", "-rf", "--one-file-system", str(folder)], check=False)
            continue
        with contextlib.suppress(OSError):
            folder.rmdir()  # only when empty


INBOX_EMPTY = b"homelab-ci-inbox-empty\n"


def write_inbox(inbox: Path, nonce: str, verdict: str, item: Path | None = None) -> None:
    """Answer an ask on the VM's inbox: blank the header, write the item's tar after 512
    bytes, then the header, so the job never reads a half-written tar as an answer."""
    with inbox.open("r+b") as f:
        f.write(INBOX_EMPTY.ljust(512, b"\0"))
        f.flush()
        os.fsync(f.fileno())
        size = 0
        if item is not None:
            f.seek(512)
            with item.open("rb") as source:
                while chunk := source.read(1024**2):
                    f.write(chunk)
                    size += len(chunk)
            f.flush()
            os.fsync(f.fileno())
        f.seek(0)
        f.write(f"homelab-ci-inbox v1 {nonce} {verdict} {size}\n".encode().ljust(512, b"\0"))
        f.flush()
        os.fsync(f.fileno())


@dataclass(frozen=True)
class Asker:
    """The job running on a VM, as GitHub shows it, and whether its run may read other runs'
    items: only a run of the default branch, never a pull request's, whose workflow file a
    branch could have changed to give a job any name."""

    job: str
    trusted: bool


def asking_job(gh: GitHub, repo: str, runner: str, run_id: int, attempt: int) -> Asker | None:
    path = f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}"
    jobs = gh.request("GET", f"{path}/jobs?per_page=100").get("jobs", [])
    job = next((j for j in jobs if j.get("runner_name") == runner), None)
    if not (job and job.get("status") == "in_progress"):
        return None
    run = gh.request("GET", path)
    default = gh.request("GET", f"/repos/{repo}").get("default_branch")
    event = str(run.get("event", ""))
    trusted = run.get("head_branch") == default and not event.startswith("pull_request")
    return Asker(str(job["name"]), trusted)


def writer_state(gh: GitHub, repo: str, run_id: int, job_name: str) -> tuple[int, bool]:
    """The latest attempt of the job that would put the item, and whether that attempt's
    items may still come: until it has finished and its VM's outbox is filed."""
    path = f"/repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"
    jobs = gh.request("GET", path).get("jobs", [])
    job = next((j for j in jobs if j.get("name") == job_name), None)
    if not job:
        return 0, False
    coming = job.get("status") != "completed"
    coming = coming or (OUTBOX_DIR / f"{job.get('runner_name')}.json").exists()
    return int(job.get("run_attempt") or 0), coming


def answer_ask(
    gh: GitHub,
    repo: str,
    runner: str,
    inbox: Path,
    ask: re.Match[bytes],
    known: dict[tuple[int, int], Asker | None],
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Answer one ask on the inbox: ok (the item), none (it is not there and will not come
    within the wait) or denied (the asking job is not one the item names as a reader).
    Returns the verdict."""
    nonce = ask[1].decode()
    own = (int(ask[2]), int(ask[3]))
    run_id, job_name, item = int(ask[4]), ask[5].decode(), ask[6].decode()
    wait = min(int(ask[7]), HANDOFF_MAX_WAIT_SECONDS)

    def answer(verdict: str, path: Path | None = None) -> str:
        write_inbox(inbox, nonce, verdict, path)
        return verdict

    if not (HANDOFF_JOB.fullmatch(job_name) and HANDOFF_ITEM.fullmatch(item)):
        return answer("denied")
    # A VM runs one job: once GitHub has named it, no other run is looked up.
    if own not in known and not any(known.values()):
        known[own] = asking_job(gh, repo, runner, *own)
    me = known.get(own)
    if me is None:
        return answer("denied")
    meta = repo_handoffs(repo) / str(run_id) / job_name / f"{item}.json"
    deadline = clock() + wait
    latest, coming, checked = 0, True, -float(HANDOFF_STATE_SECONDS)
    while True:
        now = clock()
        if now - checked >= HANDOFF_STATE_SECONDS:
            checked = now
            latest, coming = writer_state(gh, repo, run_id, job_name)
        # Read after the writer's state: an outbox filed meanwhile is seen.
        fields = json.loads(meta.read_text()) if meta.exists() else None
        # A rerun's item replaces the last attempt's; wait for it rather than hand on the old.
        stale = fields is not None and coming and int(fields.get("attempt", 0)) < latest
        if fields is not None and not stale:
            readers = fields.get("later", []) if me.trusted else []
            readers = readers + (fields.get("readers", []) if run_id == own[0] else [])
            if me.job in readers:
                return answer("ok", meta.with_suffix(".tar"))
            return answer("denied")
        if not coming or now >= deadline:
            return answer("none")
        sleep(HANDOFF_LOOK_SECONDS)


def answer_asks(gh: GitHub, repo: str, runner: str, inbox: Path, asks: Any) -> None:
    """The VM's ask worker: one ask at a time, off the console reader, which must never wait.
    A job asks a few times; past HANDOFF_MAX_ASKS it is denied without asking GitHub, so a
    VM cannot spend the token the supervisor polls with."""
    known: dict[tuple[int, int], Asker | None] = {}
    count = 0
    while (ask := asks.get()) is not None:
        count += 1
        try:
            if count > HANDOFF_MAX_ASKS:
                write_inbox(inbox, ask[1].decode(), "denied")
                continue
            verdict = answer_ask(gh, repo, runner, inbox, ask, known)
            what = f"{ask[5].decode()}/{ask[6].decode()} of run {int(ask[4])}"
            log(f"handoff for {repo}: {runner} asked for {what}: {verdict}")
        except Exception as e:  # anything: the job waits on an answer
            log(f"handoff for {repo}: ask from {runner} failed: {type(e).__name__}: {e}")
            with contextlib.suppress(OSError):
                write_inbox(inbox, ask[1].decode(), "none")


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
    # All at once: one after another, 9 repos took 13 s a poll while a job waited (4 Nov).
    answers = state.askers.map(lambda repo: ask(gh, state, repo, now), repos)
    for repo, (jobs, took) in zip(repos, answers, strict=True):
        spent[repo] = took
        if jobs is None:
            continue
        private.append(repo)
        for job in jobs:
            if not job.ours:
                reasons[job.id] = (repo, f"no VM has its labels ({', '.join(job.labels)})")
                continue
            queued[job.want] += 1
            job_ids.setdefault(job.want, []).append(job.id)
    reap(gh, state, now, queued, private)
    maybe_take_outboxes(gh, state, now)
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
    starts = plan([w for w in oldest_first if w[1] not in LIGHT_SIZES], booting, len(free))
    light_starts = plan([w for w in oldest_first if w[1] in LIGHT_SIZES], booting, len(light_free))
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

    # Oldest job first across both kinds: a build held for memory holds back only the
    # light jobs queued after it, so a task's quick gate or merge step queued before it
    # still starts when there is room for its small VM, and neither kind starves the other.
    builds = with_job_ids(starts, job_ids, booting)
    lights = with_job_ids(light_starts, job_ids, booting)
    held: dict[int, tuple[str, str]] = {}
    oldest_held_build: int | None = None
    oldest_held_light = False
    started = light_started = 0
    for job_id, (repo, label) in sorted(builds + lights):
        if label not in LIGHT_SIZES:
            mem = next_size(state, False)[1]
            if oldest_held_build is not None or mem > room:
                if oldest_held_build is None:
                    oldest_held_build = job_id
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
            )
            room -= mem
            started += 1
        elif LIGHT_SIZES[label][1] > room:
            held[job_id] = (repo, why(LIGHT_SIZES[label][1]))
            # Small VMs fit where a 2 GiB one does not: a stream of them would starve it.
            oldest_held_light = True
            continue
        elif oldest_held_build is not None:
            held[job_id] = (repo, "an older build waiting for memory")
            continue
        elif oldest_held_light:
            held[job_id] = (repo, "an older light job waiting for memory")
            continue
        else:
            launch(gh, state, repo, light_free[light_started], now, label=label, cache=None)
            room -= LIGHT_SIZES[label][1]
            light_started += 1
        pace()
    launched = starts[:started] + light_starts[:light_started]
    hold_for_memory(state, repos, held)
    # The rest of each want's jobs: the oldest are covered by VMs on their way, the next
    # were planned (started or held above), and the rest found no free slot.
    planned = {job_id for job_id, _ in builds + lights}
    for want, ids in job_ids.items():
        kind, slots = (
            ("light", state.light_slots) if want[1] in LIGHT_SIZES else ("build", state.slots)
        )
        for n, job_id in enumerate(sorted(ids)):
            if job_id in held:
                reasons[job_id] = held[job_id]
            elif n < booting[want]:
                reasons[job_id] = (want[0], BOOTING)
            elif job_id not in planned:
                reasons[job_id] = (want[0], f"all {slots} {kind} slots in use")
    note_waits(state, private, reasons, now)
    free = free[started:]
    # A build job still waiting for a slot, or for memory, gets the standby's slot and
    # memory, if the standby cannot take it. A light job never uses a build slot.
    waiting = queued - booting
    waiting.subtract(launched)
    for run in list(state.launches.values()):
        if run.standby and any(
            n > 0 and want != run.want and (want[1] in LIGHT_SIZES) == run.light
            for want, n in waiting.items()
        ):
            stop_if_still_idle(gh, run, "is in the way of a queued job")
        # The light standby also gives its memory to a build job waiting for it, unless
        # light jobs it would serve were queued before that build.
        elif (
            run.standby
            and run.light
            and oldest_held_build is not None
            and all(oldest_held_build < i for i in job_ids.get(run.want, []))
        ):
            stop_if_still_idle(gh, run, "is in the way of a job waiting for memory")
    standbys = [r for r in state.launches.values() if r.standby]
    # A standby is the first thing given up: never booted while a job waits for memory.
    if (
        not state.held
        and next_size(state, True)[1] <= room
        and wants_standby(
            repos, private, [not r.light for r in standbys], len(free), standby_count()
        )
    ):
        repo = repos[0]
        # Booted for the last job's cache label too, else a job asking for it never takes
        # the standby and stops it as in the way (5 Nov: every task's Builder and Reviewer).
        label = state.last_label if repo == state.last_repo else ""
        cache = cache_for(repo, label, private)
        launch(gh, state, repo, free[0], now, label=label, cache=cache, standby=True)
        room -= next_size(state, True)[1]
        pace()
    # One light VM waits ready for the last light job's repo, in a free light slot.
    light_free = light_free[light_started:]
    if (
        not state.held
        and LIGHT_SIZES[state.last_light_label][1] <= room
        and wants_standby(
            [state.last_light_repo] if state.last_light_repo else [],
            private,
            [r.light for r in standbys],
            len(light_free),
            light_standby_count(),
        )
    ):
        repo = state.last_light_repo
        label = state.last_light_label
        launch(gh, state, repo, light_free[0], now, label=label, cache=None, standby=True)
        pace()
    return spent


def with_job_ids(
    starts: list[Want], job_ids: Mapping[Want, list[int]], covered: Mapping[Want, int]
) -> list[tuple[int, Want]]:
    """Each planned start with the id of the job it is for: a want's oldest jobs not
    covered by a VM already on its way, in order."""
    taken = Counter(covered)
    paired: list[tuple[int, Want]] = []
    for want in starts:
        paired.append((sorted(job_ids[want])[taken[want]], want))
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


def note_waits(
    state: State, repos: list[str], reasons: Mapping[int, tuple[str, str]], now: float
) -> None:
    """Log each queued job not started, with why and how long it has waited here: when it
    starts waiting and every WAIT_LOG_SECONDS. A job that was logged gets one more line when
    it stops waiting. Jobs of repos not polled this time are left as they are."""
    for job_id, wait in list(state.waits.items()):
        if job_id not in reasons and wait.repo in repos:
            del state.waits[job_id]
            if wait.logged is not None:
                waited = duration(now - wait.since)
                log(f"job {job_id} in {wait.repo} stopped waiting after {waited}")
    for job_id, (repo, why) in sorted(reasons.items()):
        wait = state.waits.setdefault(job_id, Wait(repo, now))
        waited = now - wait.since
        quiet = why == BOOTING and waited < WAIT_LOG_SECONDS
        if not quiet and (wait.logged is None or now - wait.logged >= WAIT_LOG_SECONDS):
            log(f"job {job_id} in {repo} has waited {duration(waited)} here: {why}")
            wait.logged = now


def wants_standby(
    repos: list[str], private: list[str], standbys: list[bool], free: int, wanted: int = 1
) -> bool:
    """Up to `wanted` standby VMs for the first repo (the last job's), while private and free."""
    return bool(repos) and repos[0] in private and sum(standbys) < wanted and free > 0


def standby_count() -> int:
    return count_in(STANDBY_COUNT_FILE)


def light_standby_count() -> int:
    return count_in(LIGHT_STANDBY_COUNT_FILE)


def count_in(path: Path) -> int:
    """How many standbys a host file asks for: 1 if it is missing or not a number."""
    try:
        return max(0, int(path.read_text().strip()))
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
    runs = state.launches.values()
    standby = sum(run.standby and not run.light for run in runs)
    light_standby = sum(run.standby and run.light for run in runs)
    light = sum(run.light for run in runs) - light_standby
    return {
        "updated": int(now),
        "slots": state.slots,
        "in_use": len(state.launches) - standby - light - light_standby,
        "standby": standby,
        "light_slots": state.light_slots,
        "light_in_use": light,
        "light_standby": light_standby,
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
    # The light standby's repo too, so its privacy is checked every poll like the standby's.
    busy = {
        r for r in repos if r in state.used or r == state.last_light_repo or cache_image(r).exists()
    }
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
    OUTBOX_DIR.mkdir(mode=0o700, exist_ok=True)
    # Root's alone, like the outboxes: private repos' sessions and changes.
    HANDOFF_DIR.mkdir(mode=0o700, exist_ok=True)
    HANDOFF_DIR.chmod(0o700)
    swept = 0.0
    # Half-built caches from a supervisor that was killed mid-build.
    for leftover in CACHE_DIR.glob("tmp*"):
        subprocess.run(["rm", "-rf", "--one-file-system", str(leftover)], check=False)
    state = State(slots=slot_count(), light_slots=light_slot_count())
    # The standby waits where the last job before the restart ran, not on the first repo
    # listed (4 Nov: it booted for a repo with no work, for 45 minutes).
    state.last_repo, state.last_label = recalled_last_job()
    if state.last_repo:
        state.used.add(state.last_repo)
    state.last_light_repo, state.last_light_label = recalled_last_light_job()
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
            if time.time() - swept > HANDOFF_SWEEP_SECONDS:
                swept = time.time()
                sweep_handoffs(swept)
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
