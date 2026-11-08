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
import grp
import hashlib
import http.client
import io
import json
import os
import queue
import re
import shutil
import signal
import socket
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
# Light jobs (Auto Kaizen's gate, merge, Watcher and other small jobs) get small VMs in
# slots of their own, after the build slots, so they never hold up a build and a merge
# waiting on CI never takes the slot that CI needs. A light VM registers without LABEL,
# so GitHub never gives it a build job.
LIGHT_LABEL = "homelab-fc-light"
# Light jobs that run no agent ask for this instead and get half the memory: they peak
# under 700 MiB, so more of them fit beside the builds.
SMALL_LABEL = "homelab-fc-small"
# A merge's deploy job asks for this: small, with a standby of its own that no task job can
# take, so a deploy never waits behind them for a VM.
DEPLOY_LABEL = "homelab-fc-deploy"
LIGHT_SLOTS_FILE = Path("/etc/homelab-ci/light-slots")
# None until install-host.sh writes the file, as it also makes their users and taps.
DEFAULT_LIGHT_SLOTS = 0
# vCPUs and MiB by label. One core, as they mostly wait; 2 GiB for jobs running Claude
# Code with no swap.
LIGHT_SIZES = {LIGHT_LABEL: (1, 2048), SMALL_LABEL: (1, 1024), DEPLOY_LABEL: (1, 1024)}
# A VM's kind in logs and metrics, by the label it took; any other label is a build VM.
KINDS = {SMALL_LABEL: "small", LIGHT_LABEL: "light", DEPLOY_LABEL: "deploy"}


def light_runner_labels(label: str) -> tuple[str, ...]:
    return ("self-hosted", "linux", "x64", label)


# A merge job waits for a pull request's CI, two rounds at most.
LIGHT_TIMEOUT_SECONDS = 3 * 3600
# How many light VMs wait ready for the last 2 GiB light job's repo, so a task's agent
# steps start in seconds. /etc/homelab-ci/light-standbys changes it with no code change.
LIGHT_STANDBY_COUNT_FILE = Path("/etc/homelab-ci/light-standbys")
LAST_LIGHT_REPO_FILE = Path("/var/lib/homelab-ci/last-light-repo")
# Likewise for the deploy standby, which waits for the last deploy job's repo.
DEPLOY_STANDBY_COUNT_FILE = Path("/etc/homelab-ci/deploy-standbys")
LAST_DEPLOY_REPO_FILE = Path("/var/lib/homelab-ci/last-deploy-repo")
# And for the small standby, for the last small job's repo: each task's first step is one.
SMALL_STANDBY_COUNT_FILE = Path("/etc/homelab-ci/small-standbys")
LAST_SMALL_REPO_FILE = Path("/var/lib/homelab-ci/last-small-repo")
# Where the deploy and small standbys wait until a job of theirs has run.
DEFAULT_DEPLOY_REPO = "ColinCee/auto-kaizen"
# The labels whose standby keeps a light slot of its own and boots while jobs wait for
# memory: a merge's deploy, and the short jobs that run no agent (without it a task's first
# step waited for a VM a typical 55 s, most of it behind builds' memory and full light slots).
KEPT_LABELS = (DEPLOY_LABEL, SMALL_LABEL)
KEPT_FOR = {DEPLOY_LABEL: "deploys", SMALL_LABEL: "small jobs"}
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
# Build VMs are kept booted and registered for the jobs likely next (see standby_wants), so
# a job starts in seconds instead of waiting for a VM. Each is replaced every 45 minutes
# while idle; its timeout leaves a job that starts just before then time to finish.
STANDBY_RECYCLE_SECONDS = 45 * 60
# How many build VMs wait ready at once, each for a different repo and cache label (each
# about 450 MB while idle). Read from /etc/homelab-ci/standbys if present (see standby_count).
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
# A job asks how many build machines are free with this line (ci-capacity); the answer,
# counts only (capacity()), goes on the inbox as JSON in place of a tar, and counts as an ask.
CAPACITY_ASK = re.compile(rb"HOMELAB-CAPACITY 1 ([a-z0-9]{8,32})\r?\n?")
capacity_ask = CAPACITY_ASK.fullmatch
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
# What the job really needed (cache left out), the VM's memory and the kernel's out-of-memory
# kills, from the VM's ci-memwatch. A job can print it too: it only skews the counts.
MEM_LINE = re.compile(rb"HOMELAB-MEM 1 ([0-9]{1,7}) ([0-9]{1,7}) ([0-9]{1,9})")
# Counts by kind of VM for Grafana (Alloy's textfile collector), and the share of its memory a
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
# wait (see wait_kind) and the reason of its longest kind, for the Control room and speed
# reports. Lines older than WAITS_KEEP_SECONDS are dropped at most every WAITS_PRUNE_SECONDS.
# "unseen" is the time from GitHub queueing the job to the supervisor first seeing it (a
# restart counts again from GitHub's queueing).
WAITS_FILE = STATUS_FILE.with_name("waits.jsonl")
WAIT_KINDS = ("memory", "disk", "slots", "booting", "ready", "unseen", "other")
WAITS_KEEP_SECONDS = 30 * 24 * 3600
WAITS_PRUNE_SECONDS = 3600.0
# The longest-waiting queued job's seconds so far per kind of wait, for the "stuck waiting
# for a test VM" alert (Alloy's textfile collector), at most every WAIT_METRICS_SECONDS: a
# write every 2 s poll would only add load to the shared disk.
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
    # GitHub showed its runner online and idle: a job it covers waits on GitHub, not the boot.
    ready: bool = False

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
    def memory_file(self) -> Path:
        """Its latest memory line, written by its console reader."""
        return self.live_file.with_suffix(".mem")

    @property
    def kind(self) -> str:
        return KINDS.get(self.label, "build")

    @property
    def want(self) -> Want:
        return (self.repo, self.label)

    @property
    def light(self) -> bool:
        return self.label in LIGHT_SIZES

    def takes(self, now: float) -> tuple[Want, ...]:
        """The wants whose queued jobs it may take. The light standby registers with the small
        label too, so a small job of its repo starts on it instead of booting a VM."""
        if self.label == LIGHT_LABEL and (self.standby or now < self.covers_until):
            return (self.want, (self.repo, SMALL_LABEL))
        return (self.want,)


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
    # The repository of the last 2 GiB light job: the light standby boots for it.
    last_light_repo: str = ""
    # The repository of the last deploy job, and of the last small one: their standbys boot there.
    last_deploy_repo: str = DEFAULT_DEPLOY_REPO
    last_small_repo: str = DEFAULT_DEPLOY_REPO
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
        run_jobs = list(get_jobs(int(run["id"])))
        run_started = any(
            job.get("status") == "completed" and on_build_label(job.get("labels", []))
            for job in run_jobs
        )
        for job in run_jobs:
            labels = tuple(str(label) for label in job.get("labels", []))
            if job.get("status") != "queued":
                continue
            ours = wants_us(labels)
            # One asking for our label with another we lack is kept, to log why it waits.
            if ours or {LABEL, *LIGHT_SIZES} & {label.lower() for label in labels}:
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
    asked = {str(label).lower() for label in labels}
    return LABEL in asked and not LIGHT_SIZES.keys() & asked


def start_order(jobs: Iterable[Job], waited_since: Callable[[int], float], now: float) -> list[Job]:
    """Jobs in the order they get slots and memory: oldest first, except that a new run's
    first build job waits behind later jobs of runs already started (a task's review), so a
    task half done is not left behind newer tasks' builds. A first build queued over
    FIRST_BUILD_WAIT_MAX goes back to oldest first."""

    def new(job: Job) -> bool:
        if job.run_started or not on_build_label(job.labels):
            return False
        return now - (job.queued_at or waited_since(job.id)) < FIRST_BUILD_WAIT_MAX

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
    light = label in LIGHT_SIZES
    # The cache label is registered too, so a job asking for it runs only on such a VM.
    if light:
        labels = list(light_runner_labels(label))
        if standby and label == LIGHT_LABEL:
            labels.append(SMALL_LABEL)
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
    if not standby and not light:
        note_last_job(state, repo, label)
    if label == DEPLOY_LABEL:
        if not standby and repo != state.last_deploy_repo:
            state.last_deploy_repo = repo
            remember(LAST_DEPLOY_REPO_FILE, f"{repo}\n")
    elif label == SMALL_LABEL:
        if not standby and repo != state.last_small_repo:
            state.last_small_repo = repo
            remember(LAST_SMALL_REPO_FILE, f"{repo}\n")
    elif light and not standby and repo != state.last_light_repo:
        state.last_light_repo = repo
        remember(LAST_LIGHT_REPO_FILE, f"{repo}\n")
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


def start_vm(run: Launch, timeout: int, env: Mapping[str, str], peer: bool = False) -> None:
    """Start the VM as a systemd unit of its own, so a restart of this supervisor leaves it
    running. Its console goes through this script's `vm` mode, which keeps every VM's memory
    line, and the live lines and sessions of auto-kaizen's VMs only, so a product's VM cannot put
    a line on a task. The
    unit stops before the CI network does, at shutdown."""
    vm = [str(run.slot), str(run.metadata), str(timeout)]
    vm += ["--repo", run.repo, "--runner", run.runner_name]
    if peer:
        vm.append("--peer")
    else:
        vm += ["--memory", str(run.memory_file)]
    if not peer and run.repo.lower() == LIVE_REPO.lower():
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
    """A VM's files, once it has ended, its memory counted first."""
    count_memory(run)
    for path in (run.record, run.live_file, run.memory_file, run.metadata):
        path.unlink(missing_ok=True)


# Ended job VMs, near-full ones and out-of-memory kills, by kind; every kind from 0, so the
# first kill after a restart still shows as an increase.
MEMORY_COUNTS: dict[str, list[int]] = {kind: [0, 0, 0] for kind in ("build", *KINDS.values())}


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
        f"slot {run.slot}: {run.runner_name} ({run.kind}) needed at least {peak} MiB of"
        f" {total}, {kills} out-of-memory kills"
    )
    counts = MEMORY_COUNTS.setdefault(run.kind, [0, 0, 0])
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
    """`vm SLOT METADATA TIMEOUT [--memory FILE] [--live FILE --session FILE]
    [--repo R --runner N]`: run-vm.sh's console reader, the main process of the VM's unit. It
    reads the console to the end (a full pipe would stall the VM), keeps the latest memory line
    and live line in their FILEs and the session events, hands handoff asks to a worker that
    answers them on the VM's inbox, and exits with run-vm.sh's code."""
    parser = argparse.ArgumentParser(prog="ci_supervisor.py vm")
    parser.add_argument("slot")
    parser.add_argument("metadata")
    parser.add_argument("timeout")
    parser.add_argument("--live", type=Path)
    # Every VM's memory line, whatever its repo.
    parser.add_argument("--memory", type=Path)
    parser.add_argument("--session", type=Path)
    # Whose VM it is, to answer its handoff asks.
    parser.add_argument("--repo")
    parser.add_argument("--runner")
    # On the PC: the lines and asks go to the Beelink (PeerStream), which holds the token.
    parser.add_argument("--peer", action="store_true")
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
    memory = (lambda seen: write_status(args.memory, seen)) if args.memory else None
    asks: queue.Queue[re.Match[bytes] | None] = queue.Queue(HANDOFF_MAX_ASKS)

    def hand_on(ask: re.Match[bytes]) -> None:
        with contextlib.suppress(queue.Full):  # the reader must never wait
            asks.put_nowait(ask)

    inbox = OUTBOX_DIR / f"{args.runner}.inbox"
    if args.peer:
        stream = PeerStream(args.runner, inbox)
        try:
            read_console(process.stdout, None, forward=stream.put)
            return process.wait()
        finally:
            stream.close()
            inbox.unlink(missing_ok=True)
    if args.repo and args.runner:
        gh = GitHub(TOKEN_FILE.read_text().strip())
        threading.Thread(
            target=answer_asks, args=(gh, args.repo, args.runner, inbox, asks), daemon=True
        ).start()
    try:
        read_console(
            process.stdout, keep, args.session, hand_on if args.runner else None, memory=memory
        )
        return process.wait()
    finally:
        with contextlib.suppress(queue.Full):
            asks.put_nowait(None)
        inbox.unlink(missing_ok=True)


def memory_line(raw: bytes) -> dict[str, int] | None:
    """One console line as ci-memwatch's report, or None unless it is exactly that."""
    match = MEM_LINE.fullmatch(raw.removesuffix(b"\n").removesuffix(b"\r"))
    if not match:
        return None
    return {"peak": int(match[1]), "total": int(match[2]), "kills": int(match[3])}


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
    forward: Callable[[bytes], None] | None = None,
    memory: Callable[[dict[str, int]], None] | None = None,
) -> None:
    """Read a VM's console until it ends, keeping each valid live line and saving each
    session event to `session`, or, on the PC, handing each line that may be one of them
    or an ask to `forward` (the Beelink checks them again). It must never stop early: once
    the pipe fills, the VM's console, and with it the job, would stall; so a failed save is
    logged once and reading goes on. A line over CONSOLE_LINE_MAX bytes is dropped, to its
    end, without being held in memory."""
    with stream:
        too_long, failed = False, False
        while chunk := stream.readline(CONSOLE_LINE_MAX):
            whole = chunk.endswith(b"\n")
            fits = not too_long and (whole or len(chunk) < CONSOLE_LINE_MAX)
            if forward and fits and whole and chunk.startswith(FORWARDED):
                forward(chunk)
            elif ask and fits and (asked := HANDOFF_ASK.fullmatch(chunk) or capacity_ask(chunk)):
                ask(asked)
            elif keep and fits and (line := live_line(chunk, time.time())):
                keep(line)
            elif memory and fits and (seen := memory_line(chunk)):
                memory(seen)
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
                    if not run.light:
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
        if not run.standby and not run.light:
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
    if fields.get("light") or runner.startswith(PEER_PREFIX):
        # A PC VM's outbox passed through the PC guest, which could have changed it.
        log(f"cache for {repo}: outbox of {runner} ignored, only Beelink build VMs make caches")
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
    records = [*OUTBOX_DIR.glob(f"{RUNNER_PREFIX}*.json"), *OUTBOX_DIR.glob(f"{PEER_PREFIX}*.json")]
    for record in records:
        if record.stem in running:
            continue
        tar = record.with_suffix(".tar")
        # A PC VM's record stands from its registration (writer_state reads it as "its items
        # may still come") until its outbox is uploaded, or sweep_peer gives up on it.
        if record.name.startswith(PEER_PREFIX) and not tar.exists():
            continue
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


def write_inbox(
    inbox: Path, nonce: str, verdict: str, item: Path | None = None, data: bytes = b""
) -> None:
    """Answer an ask on the VM's inbox: blank the header, write the item's tar (or `data`)
    after 512 bytes, then the header, so the job never reads a half-written answer."""
    with inbox.open("r+b") as f:
        f.write(INBOX_EMPTY.ljust(512, b"\0"))
        f.flush()
        os.fsync(f.fileno())
        size = 0
        if data:
            f.seek(512)
            f.write(data)
            size = len(data)
            f.flush()
            os.fsync(f.fileno())
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
    # A VM runs one job: once GitHub has named it, no other run is looked up. A PC VM gets one
    # lookup in all, so a stream from the door with no job behind it costs one request.
    looked = bool(known) if runner.startswith(PEER_PREFIX) else any(known.values())
    if own not in known and not looked:
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


def answer_asks(
    gh: GitHub,
    repo: str,
    runner: str,
    inbox: Path,
    asks: Any,
    count: int = 0,
    answered: Callable[[], None] | None = None,
) -> None:
    """The VM's ask worker: one ask at a time, off the console reader, which must never wait.
    A job asks a few times; past HANDOFF_MAX_ASKS it is denied without asking GitHub, so a
    VM cannot spend the token the supervisor polls with. `count` is the asks it has made
    already (a PC VM's stream may reconnect); `answered` runs after each answer."""
    known: dict[tuple[int, int], Asker | None] = {}
    while (ask := asks.get()) is not None:
        count += 1
        try:
            if count > HANDOFF_MAX_ASKS:
                write_inbox(inbox, ask[1].decode(), "denied")
            elif ask.re is CAPACITY_ASK:
                counts = capacity(STATUS_FILE, repo)
                answer = json.dumps(counts).encode() if counts else b""
                write_inbox(inbox, ask[1].decode(), "ok" if counts else "none", data=answer)
            else:
                verdict = answer_ask(gh, repo, runner, inbox, ask, known)
                what = f"{ask[5].decode()}/{ask[6].decode()} of run {int(ask[4])}"
                log(f"handoff for {repo}: {runner} asked for {what}: {verdict}")
        except Exception as e:  # anything: the job waits on an answer
            log(f"handoff for {repo}: ask from {runner} failed: {type(e).__name__}: {e}")
            with contextlib.suppress(OSError):
                write_inbox(inbox, ask[1].decode(), "none")
        if answered:
            try:
                answered()
            except OSError as e:  # the PC's link is gone: it asks again when it is back
                log(f"handoff for {repo}: answer to {runner} not sent: {e}")
                return


CAPACITY_FIELDS = ("updated", "slots", "in_use", "standby", "queued", "held_for_memory")


def capacity(machines: Path, repo: str) -> dict[str, int] | None:
    """The build machines' counts from the status file, for a job of `repo` asking: no repo
    names, so any job may have them. None if the file is missing or not the supervisor's."""
    try:
        fields = json.loads(machines.read_text())
        counts = {name: int(fields[name]) for name in CAPACITY_FIELDS[:2]}
        counts |= {name: int(fields.get(name, 0)) for name in CAPACITY_FIELDS[2:]}
        by_repo = {str(r).lower(): int(n) for r, n in (fields.get("in_use_repos") or {}).items()}
        counts["in_use_other_repos"] = counts["in_use"] - by_repo.get(repo.lower(), 0)
        pc = fields.get("pc") or {}
        counts |= {
            "pc_up": int(bool(pc.get("up"))),
            "pc_slots": int(pc.get("slots", 0)),
            "pc_in_use": int(pc.get("in_use", 0)),
        }
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return None
    return counts


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
    # The light standby covers a small job of its repo when no light job needs it.
    for run in state.launches.values():
        for small in run.takes(now)[1:]:
            if booting[run.want] > queued[run.want] and queued[small] > booting[small]:
                booting[run.want] -= 1
                booting[small] += 1
                if run.ready:
                    ready[run.want] -= 1
                    ready[small] += 1
    # So do the PC's idle ready runners, for jobs queued under PEER_COVER_SECONDS: GitHub hands
    # each job to one idle runner, and booting for it here too would waste a VM.
    pc_ready = peer_ready(now) if PEER else Counter()
    for want, n in pc_ready.items():
        young = [
            i
            for i in job_ids.get(want, [])
            if now - waited_since(state, i, now) < PEER_COVER_SECONDS
        ]
        booting[want] += min(n, len(young))
        ready[want] += min(n, len(young))
    draining = DRAIN_FILE.exists()
    if state.draining and not draining:
        # A failed load test lowers the count while no VM runs; never raised here, as more
        # slots need install-host.sh's users and taps.
        state.slots = min(state.slots, slot_count())
    state.draining = draining
    free = [slot for slot in range(state.slots) if slot not in state.launches and not draining]
    light_free = [
        slot
        for slot in range(state.slots, state.slots + state.light_slots)
        if slot not in state.launches and not draining
    ]
    # GitHub's job ids only grow, so the smallest is the oldest.
    order = start_order(polled, lambda i: waited_since(state, i, now), now)
    rank = {(job.id, job.want): n for n, job in enumerate(order)}
    in_order = [job.want for job in order]
    starts = plan([w for w in in_order if w[1] not in LIGHT_SIZES], booting, len(free))
    # Deploys first, in any free light slot, then small jobs, then 2 GiB ones; none takes
    # a slot kept for another label's standby.
    deploy_starts = plan([w for w in in_order if w[1] == DEPLOY_LABEL], booting, len(light_free))
    light_starts = deploy_starts
    for label in (SMALL_LABEL, LIGHT_LABEL):
        room_left = light_room(state, label, Counter(w[1] for w in light_starts))
        light_starts = light_starts + plan(
            [w for w in in_order if w[1] == label],
            booting,
            min(len(light_free) - len(light_starts), room_left),
        )
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

    # In start order. A light job whose VM fits starts even past a build held for memory:
    # held behind it, a task's first step and merge step (which run no build) waited for
    # minutes with room for their VMs. Light VMs never outgrow their own slots, so a build
    # waits behind at most those, and builds still start in start_order (a started run's
    # job first, oldest first within each group, a new build back in after
    # FIRST_BUILD_WAIT_MAX).
    builds = with_job_ids(starts, job_ids, booting)
    lights = with_job_ids(light_starts, job_ids, booting)
    held: dict[int, tuple[str, str]] = {}
    first_held_build: int | None = None
    oldest_held_light = False
    started = light_started = 0
    for job_id, (repo, label) in sorted(builds + lights, key=rank.__getitem__):
        if label not in LIGHT_SIZES:
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
        elif LIGHT_SIZES[label][1] > room:
            held[job_id] = (repo, why(LIGHT_SIZES[label][1]))
            # Small VMs fit where a 2 GiB one does not: a stream of them would starve it.
            oldest_held_light = True
            continue
        elif oldest_held_light:
            held[job_id] = (repo, "an older light job waiting for memory")
            continue
        else:
            launch(
                gh,
                state,
                repo,
                light_free[light_started],
                now,
                label=label,
                cache=None,
                jobs=job_ids[(repo, label)],
            )
            room -= LIGHT_SIZES[label][1]
            light_started += 1
        pace()
    launched = starts[:started] + light_starts[:light_started]
    hold_for_memory(state, repos, held)
    # The rest of each want's jobs: the oldest are covered by VMs on their way or started
    # above, and the rest found no free slot.
    planned = {job_id for job_id, _ in builds + lights}
    for want, ids in job_ids.items():
        kind, slots = (
            ("light", state.light_slots) if want[1] in LIGHT_SIZES else ("build", state.slots)
        )
        kept = (
            kept_from(state, want[1]) if want[1] in LIGHT_SIZES and want[1] != DEPLOY_LABEL else []
        )
        full = f"all {slots - len(kept)} {kind} slots in use" + "".join(
            f", one more kept for {KEPT_FOR[k]}" for k in kept
        )
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
    # memory, if the standby cannot take it. A light job never uses a build slot.
    waiting = queued - booting
    waiting.subtract(launched)
    if PEER:
        builds_waiting = (w for w in in_order if w[1] not in LIGHT_SIZES and waiting[w] > 0)
        PEER.wants = tuple(dict.fromkeys(builds_waiting))
        # The queued Auto Kaizen build's last in start order, kept until another is queued: the
        # label a ready PC runner most likely needs next (each task's build asks for its product's).
        ours = [
            w for w in in_order if w[1] not in LIGHT_SIZES and w[0].lower() == LIVE_REPO.lower()
        ]
        if ours:
            PEER.last = ours[-1]
    for run in list(state.launches.values()):
        # The deploy and small standbys are the VMs those jobs need: each gives way only to
        # another repo's job of its label, never to other jobs or memory (no other label takes
        # its slot).
        kept = run.label in KEPT_LABELS
        if run.standby and any(
            n > 0
            and want not in run.takes(now)
            and ((want[1] == run.label) if kept else (want[1] in LIGHT_SIZES) == run.light)
            for want, n in waiting.items()
        ):
            stop_if_still_idle(gh, run, "is in the way of a queued job")
        # The light standby also gives its memory to a build job waiting for it, unless
        # light jobs it would serve were queued before that build.
        elif (
            run.standby
            and run.light
            and not kept
            and first_held_build is not None
            and all(first_held_build < i for w in run.takes(now) for i in job_ids.get(w, []))
        ):
            stop_if_still_idle(gh, run, "is in the way of a job waiting for memory")
    if draining:
        for run in [r for r in state.launches.values() if r.standby]:
            stop_if_still_idle(gh, run, "gives way to the host's load test")
        return spent
    standbys = [r for r in state.launches.values() if r.standby]
    # A standby is the first thing given up: never booted while a job waits for memory, nor
    # while a build job waits for a slot (one at a time while free slots outnumber them).
    # Nor for a want one of the PC's ready runners waits for: it is the standby.
    build_standbys = [r for r in standbys if not r.light]
    waiting_builds = sum(n for w, n in waiting.items() if n > 0 and w[1] not in LIGHT_SIZES)
    target = next(
        (
            w
            for w in standby_wants(state, repos, private)
            if not pc_ready[w] and all(r.want != w for r in build_standbys)
        ),
        None,
    )
    if (
        target
        and not state.held
        and len(free) > waiting_builds
        and len(build_standbys) < standby_count()
        and next_size(state, True)[1] <= room
    ):
        repo, label = target
        cache = cache_for(repo, label, private)
        launch(gh, state, repo, free[0], now, label=label, cache=cache, standby=True)
        room -= next_size(state, True)[1]
        pace()
    # One light VM waits ready for the last 2 GiB light job's repo, in a free light slot.
    light_free = light_free[light_started:]
    if (
        not state.held
        and LIGHT_SIZES[LIGHT_LABEL][1] <= room
        and wants_standby(
            [state.last_light_repo] if state.last_light_repo else [],
            private,
            [r.label == LIGHT_LABEL for r in standbys],
            min(len(light_free), light_room(state, LIGHT_LABEL)),
            light_standby_count(),
        )
    ):
        repo = state.last_light_repo
        launch(gh, state, repo, light_free[0], now, label=LIGHT_LABEL, cache=None, standby=True)
        light_free = light_free[1:]
        room -= LIGHT_SIZES[LIGHT_LABEL][1]
        pace()
    # And one each for the last deploy job's repo and the last small job's, registered for
    # their label alone, so no other job takes it just before one of theirs needs it. They
    # boot even while jobs wait for memory, if their 1 GiB fits: without that a merge's
    # deploy waited up to 135 s for a VM, and a task's first step behind builds' memory.
    for label in KEPT_LABELS:
        repo = kept_repo(state, label)
        if LIGHT_SIZES[label][1] <= room and wants_standby(
            [repo],
            private,
            [r.label == label for r in standbys],
            min(len(light_free), light_room(state, label)),
            kept_standby_count(label),
        ):
            launch(gh, state, repo, light_free[0], now, label=label, cache=None, standby=True)
            light_free = light_free[1:]
            room -= LIGHT_SIZES[label][1]
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
        f"homelab_ci_vm_slots {state.slots + state.light_slots}",
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
    """Counts failed polls in a row for the "CI supervisor's poll keeps failing" alert: no VM
    starts while every poll fails. The metrics are written at once when polls start or stop
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


def wants_standby(
    repos: list[str], private: list[str], standbys: list[bool], free: int, wanted: int = 1
) -> bool:
    """Up to `wanted` standby VMs for the first repo (the last job's), while private and free."""
    return bool(repos) and repos[0] in private and sum(standbys) < wanted and free > 0


def standby_wants(state: State, repos: list[str], private: list[str]) -> list[Want]:
    """The repos and cache labels build standbys boot for, most needed first: the last build
    job's (or the first polled repo's) once no VM runs it, then each running build job's,
    oldest first (a task's Reviewer asks for its Builder's seconds after the Builder ends, even
    when another task's build started in between). Booted for the cache label too, else a job
    asking for it never takes the standby and stops it as in the way."""
    running = sorted(
        (r for r in state.launches.values() if not r.standby and not r.light),
        key=lambda r: r.started,
    )
    wants = [r.want for r in running]
    if repos:
        last = (repos[0], state.last_label if repos[0] == state.last_repo else "")
        # The last build job's want leads once its VM has ended: that is when it is asked for.
        wants = [*wants, last] if last in wants else [last, *wants]
    return [w for w in dict.fromkeys(wants) if w[0] in private]


def standby_count() -> int:
    """How many build standbys may wait, each for a different repo and cache label: 2 unless
    /etc/homelab-ci/standbys says otherwise."""
    return count_in(STANDBY_COUNT_FILE, default=2)


def light_standby_count() -> int:
    return count_in(LIGHT_STANDBY_COUNT_FILE)


def deploy_standby_count() -> int:
    return count_in(DEPLOY_STANDBY_COUNT_FILE)


def small_standby_count() -> int:
    return count_in(SMALL_STANDBY_COUNT_FILE)


def kept_standby_count(label: str) -> int:
    return deploy_standby_count() if label == DEPLOY_LABEL else small_standby_count()


def kept_repo(state: State, label: str) -> str:
    return state.last_deploy_repo if label == DEPLOY_LABEL else state.last_small_repo


def kept_slots(state: State) -> list[str]:
    """The labels keeping a light slot for their standby: each whose standby is wanted, while
    another light slot is left for 2 GiB jobs."""
    kept: list[str] = []
    for label in KEPT_LABELS:
        if kept_standby_count(label) > 0 and state.light_slots > len(kept) + 1:
            kept.append(label)
    return kept


def kept_from(state: State, label: str) -> list[str]:
    """The other kept labels whose slot `label` may not take now: those with no standby up.
    A standby that takes a job keeps no slot, so the next one boots in another."""
    standing = {r.label for r in state.launches.values() if r.standby}
    return [k for k in kept_slots(state) if k != label and k not in standing]


def light_room(state: State, label: str, planned: Counter[str] | None = None) -> int:
    """How many more light VMs with `label` may start: the light slots, less those every
    light VM (and `planned`) holds, less those kept from it."""
    used = sum(r.light for r in state.launches.values()) + sum((planned or Counter()).values())
    return max(state.light_slots - used - len(kept_from(state, label)), 0)


def count_in(path: Path, default: int = 1) -> int:
    """How many standbys a host file asks for: `default` if it is missing or not a number."""
    try:
        return max(0, int(path.read_text().strip()))
    except (OSError, ValueError):
        return default


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
    deploy_standby = sum(run.standby and run.label == DEPLOY_LABEL for run in runs)
    small_standby = sum(run.standby and run.label == SMALL_LABEL for run in runs)
    light_standby = sum(run.standby and run.light for run in runs) - deploy_standby - small_standby
    light = sum(run.light for run in runs) - light_standby - deploy_standby - small_standby
    return {
        "updated": int(now),
        "slots": state.slots,
        "in_use": len(state.launches)
        - standby
        - light
        - light_standby
        - deploy_standby
        - small_standby,
        "standby": standby,
        "light_slots": state.light_slots,
        "light_in_use": light,
        "light_standby": light_standby,
        "deploy_standby": deploy_standby,
        "small_standby": small_standby,
        "cache_disks": sorted(
            image.stem.replace("__", "/", 1) for image in CACHE_DIR.glob("*__*.ext4")
        ),
        "github_ok": github_ok,
        "mem_available_mib": mem_available,
        "held_for_memory": len(state.held),
        "queued": len(state.waits),
        "in_use_repos": dict(Counter(run.repo for run in runs if not (run.standby or run.light))),
        "draining": state.draining,
        "load_test": read_load_test(),
        **({"pc": peer_status(PEER, now)} if PEER else {}),
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
    return {"updated": int(now), "lines": lines + peer_live(now)}


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
    # The light, deploy and small standbys' repos too, so their privacy is checked every poll like
    # the standby's.
    standing = {state.last_light_repo, state.last_deploy_repo, state.last_small_repo}
    busy = {r for r in repos if r in state.used or r in standing or cache_image(r).exists()}
    quiet = [
        r for r in repos if r not in busy and now - state.polled.get(r, 0) >= QUIET_POLL_SECONDS
    ]
    quiet.sort(key=lambda r: state.polled.get(r, 0))
    chosen = busy | set(quiet[:QUIET_PER_POLL])
    return [r for r in repos if r in chosen]


def standby_first(repos: list[str], last: str) -> list[str]:
    """The repo of the last job first, so the standby VM waits where work just was."""
    return [last, *(r for r in repos if r != last)] if last in repos else repos


# The PC's build slots. The PC keeps ready runners: idle VMs registered and online, one per
# free slot, so GitHub hands a build job to whichever idle runner it picks and nothing races.
# The PC holds no GitHub key: through one door (ssh ci-peer@beelink, forced to `socat` into
# PEER_SOCKET, served by this supervisor) it asks the Beelink to register each runner, and
# sends up its VMs' console lines, asks and outboxes, which the Beelink checks and files with
# the same code as its own. See docs/runbooks/ci-runner.md, "PC build slots".
PEER_PREFIX = "pc-"
PEER_RUNNER = re.compile(r"pc-([0-9]{1,2})-([0-9]{10})")
# Not under STATE_DIR, which only root may enter: ci-peer's socat must reach the socket.
PEER_SOCKET = Path("/run/homelab-ci-peer/peer.sock")
PEER_GROUP = "ci-peer"
# Each runner the Beelink registered for the PC: repo, runner id, slot, when, asks made.
PEER_DIR = Path("/var/lib/homelab-ci/peer")
# The most PC slots the Beelink registers runners for, whatever the PC says; 0 or no file:
# the door is closed.
PEER_SLOTS_FILE = Path("/etc/homelab-ci/peer-slots-max")
PEER_LINE_MAX = 256
# A reply line can carry a runner's one-job registration (several KB of base64).
PEER_REPLY_MAX = 64 * 1024
PEER_HELLO_SECONDS = 10
PEER_SWEEP_SECONDS = 60
# A PC runner still offline this long after it was registered is deleted (the PC is off).
PEER_OFFLINE_SECONDS = 600
# A PC VM's live line counts as its latest for this long after it was heard.
PEER_LIVE_SECONDS = 120
# An upload must leave this much of the Beelink's disk free.
PEER_FREE_DISK_BYTES = 20 * 1024**3
# All PC VMs' session files together; past it, new ones are not kept.
PEER_SESSIONS_MAX_BYTES = 3 * 1024**3
# A slot gets a new registration at most this often, so the PC cannot spend the token.
PEER_REGISTER_EVERY = 120
# An idle PC runner covers a queued job of its label for this long; past it the Beelink boots
# for the job, so a PC runner that never takes it holds nothing up.
PEER_COVER_SECONDS = 60
# A ready PC runner's idle state, seen by sweep_peer, counts as current for this long.
PEER_SEEN_SECONDS = 90
# An idle PC runner for one product's cache label is swapped for a queued build of another's
# once it has waited this long.
PEER_RELABEL_SECONDS = 300
# The PC says how many slots it gives every PC_SAY_SECONDS; unheard for this long, it is off.
PEER_HEARD_SECONDS = 180
# Both hosts run one commit and one job image (ci-runner/update.sh); the PC follows the Beelink's.
COMMIT_FILE = Path("/etc/homelab-ci/commit")
IMAGES_DIR = Path("/var/lib/homelab-ci/images")
# A copy of the job image (about 1.2 GB compressed): at most this many an hour, this long each,
# below the jobs on the Beelink's disk, so a PC guest asking in a loop cannot wear it down.
IMAGE_SENDS_PER_HOUR = 4
TRANSFER_SECONDS = 20 * 60
SHA256 = re.compile(r"[0-9a-f]{64}")
PC_SAY_SECONDS = 60
# run-vm.sh's outbox: 6 GiB for the tar and its header.
OUTBOX_BYTES = 6 * 1024**3 + 1024**2
# Lines the PC's console reader sends up; read_console checks them again on the Beelink.
PEER_ASKS = (b"HOMELAB-HANDOFF ", b"HOMELAB-CAPACITY ")
FORWARDED = (b"KAIZEN-LIVE ", b"KAIZEN-SESSION ", *PEER_ASKS)
# On the PC: the door, its key, the slots given now (written by the keeper on Windows, at most
# the slots installed), each VM's size.
PEER_HOST_FILE = Path("/etc/homelab-ci/peer")
PEER_KEY = Path("/etc/homelab-ci/peer-key")
PC_SLOTS_FILE = Path("/etc/homelab-ci/pc-slots")
PC_SIZE = (4, 4096)
PC_POLL_SECONDS = 5
# A job still running on a slot the count no longer has (Colin games) is stopped after this;
# the Watcher reruns it as machine trouble.
PC_DRAIN_SECONDS = 20 * 60
PC_CACHE_EVERY = 600


# The door's shared state, set by main when the door is open; tick reads it.
PEER: Peer | None = None


def peer_slots() -> int:
    try:
        text = PEER_SLOTS_FILE.read_text().strip()
    except OSError:
        return 0
    return int(text) if text.isdecimal() else 0


def peer_door_installed() -> bool:
    try:
        grp.getgrnam(PEER_GROUP)
    except KeyError:
        return False
    return True


# The door's threads and the sweep each change one field of a record: under this lock, on the
# record as it is now, so none loses another's change (the asks count is a limit).
PEER_RECORDS = threading.Lock()


def update_peer(runner: str, **changes: Any) -> dict[str, Any] | None:
    """Set fields on a PC runner's record (None drops one); the record after, or None."""
    with PEER_RECORDS:
        fields = read_peer(runner)
        if fields is None:
            return None
        for key, value in changes.items():
            if value is None:
                fields.pop(key, None)
            else:
                fields[key] = value
        write_status(peer_record(runner), fields)
        return fields


def peer_record(runner: str) -> Path:
    return PEER_DIR / f"{runner}.json"


def read_peer(runner: str) -> dict[str, Any] | None:
    """The record of a runner registered for the PC, or None for any other name."""
    if not PEER_RUNNER.fullmatch(runner):
        return None
    try:
        fields = json.loads(peer_record(runner).read_text())
    except (OSError, ValueError):
        return None
    return fields if isinstance(fields, dict) else None


def peer_cache_version(repo: str) -> str:
    """The run and attempt the repo's cache disk came from, or "" if it has none."""
    try:
        seen = json.loads(cache_image(repo).with_suffix(".json").read_text())
        return f"{int(seen['run'])}.{int(seen['attempt'])}"
    except (OSError, ValueError, KeyError, TypeError):
        return ""


@dataclass
class Peer:
    """What the door's threads share with the main loop."""

    token: str
    # The repo whose builds the PC runs, and the served repos whose cache disks it may copy,
    # replaced whole by the main loop on each listing.
    served: frozenset[str] = frozenset()
    caches: frozenset[str] = frozenset()
    # Build wants (repo, cache label) still waiting after this poll's plan, in start order, and
    # the newest Auto Kaizen build queued: a new PC runner is registered for the first that fits.
    wants: tuple[tuple[str, str], ...] = ()
    last: tuple[str, str] = ("", "")
    # The slots the PC last said it gives (0 while Colin games), and when.
    pc_slots: int = 0
    pc_heard: float = 0.0
    # Each PC VM's open stream: a new one for the same runner (the PC reconnected) ends the old.
    streaming: dict[str, socket.socket] = field(default_factory=dict)
    # Outboxes being uploaded, by runner, with their sizes: the free-disk floor counts them all.
    uploading: dict[str, int] = field(default_factory=dict)
    # One big copy (a cache disk or the job image) at a time, and when the image was last sent.
    caching: threading.Lock = field(default_factory=threading.Lock)
    image_sent: list[float] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    connections: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(2 * max(peer_slots(), 1) + 4)
    )


def serve_peer(peer: Peer) -> None:
    """The door's socket: one thread per connection, at most Peer.connections at once."""
    PEER_SOCKET.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(PEER_SOCKET.parent, 0, grp.getgrnam(PEER_GROUP).gr_gid)
    PEER_SOCKET.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(PEER_SOCKET))
    os.chmod(PEER_SOCKET, 0o660)
    os.chown(PEER_SOCKET, 0, grp.getgrnam(PEER_GROUP).gr_gid)
    server.listen(8)
    log(f"PC door open for {peer_slots()} slots")
    while True:
        conn, _ = server.accept()
        if not peer.connections.acquire(blocking=False):
            conn.close()
            continue
        threading.Thread(target=peer_connection, args=(peer, conn), daemon=True).start()


def peer_connection(peer: Peer, conn: socket.socket) -> None:
    try:
        conn.settimeout(PEER_HELLO_SECONDS)
        reader, writer = conn.makefile("rb"), conn.makefile("wb")
        words = reader.readline(PEER_LINE_MAX).decode("ascii", "replace").split()
        gh = GitHub(peer.token)
        reply = peer_answer(peer, gh, words, reader, writer, conn)
        if reply:
            writer.write(f"{reply}\n".encode())
            writer.flush()
    except Exception as e:  # anything the PC sends: logged, never the supervisor's end
        log(f"PC door: {type(e).__name__}: {e}")
    finally:
        conn.close()
        peer.connections.release()


def peer_answer(
    peer: Peer,
    gh: GitHub,
    words: list[str],
    reader: IO[bytes],
    writer: IO[bytes],
    conn: socket.socket,
) -> str:
    """One request through the door (words: its first line); returns the reply line."""
    match words:
        case ["register", slot, repo]:
            return peer_register(peer, gh, slot, repo)
        case ["vm", runner] if read_peer(runner):
            conn.settimeout(None)  # as long as the VM runs; ssh's keepalives end a dead link
            peer_vm(peer, gh, runner, reader, writer, conn)
            return ""
        case ["outbox", runner, size] if read_peer(runner) and size.isdecimal():
            return peer_outbox(peer, runner, int(size), reader, writer, conn)
        case ["slots", given] if given.isdecimal():
            peer.pc_slots, peer.pc_heard = min(int(given), peer_slots()), time.time()
            return "ok"
        case ["release", runner] if fields := read_peer(runner):
            return "gone" if peer_drop(gh, runner, fields) else "busy"
        case ["version"]:
            return " ".join(read_word(f) or "-" for f in (COMMIT_FILE, IMAGES_DIR / "sha256"))
        case ["image", have] if have == "-" or SHA256.fullmatch(have):
            return peer_image(peer, have, writer, conn)
        case ["cache", named, have] if repo := peer_cache_repo(peer, named):
            conn.settimeout(300)
            if not peer.caching.acquire(blocking=False):
                return "busy"
            try:
                return peer_cache(repo, have, writer)
            finally:
                peer.caching.release()
    return "no"


def peer_cache_repo(peer: Peer, named: str) -> str:
    """The served repo with a cache disk a PC asks for by its lowercase name, or ""."""
    return next((r for r in peer.caches if r.lower() == named), "")


def peer_wanted_label(peer: Peer, repo: str) -> str:
    """The cache label for a new PC runner: the first waiting build's in start order, else the
    last queued one's, so a ready runner fits what comes next."""
    for want_repo, label in (*peer.wants, peer.last):
        if want_repo.lower() == repo.lower() and (not label or cache_label_repo(label)):
            return label
    return ""


def peer_register(peer: Peer, gh: GitHub, slot: str, repo: str) -> str:
    """A one-job runner for a PC slot, with the build labels and the cache label of the build
    it is most likely to take (the PC mounts that cache disk if it holds a copy)."""
    if not (slot.isdecimal() and int(slot) < peer_slots() and repo in peer.served):
        return "no"
    now = time.time()
    for old in PEER_DIR.glob(f"{PEER_PREFIX}{int(slot)}-*.json"):
        fields = read_peer(old.stem)
        if not fields:
            continue
        if now - float(fields.get("issued", 0)) < PEER_REGISTER_EVERY:
            return "no"
        if not fields.get("gone") and not peer_drop(gh, old.stem, fields):
            return "no"  # its last runner still has a job
    labels = list(RUNNER_LABELS)
    label = peer_wanted_label(peer, repo)
    if label:
        labels.append(label)
    name = f"{PEER_PREFIX}{int(slot)}-{int(now)}"
    registration = gh.request(
        "POST",
        f"/repos/{repo}/actions/runners/generate-jitconfig",
        {"name": name, "runner_group_id": 1, "labels": labels, "work_folder": "_work"},
    )
    fields = {
        "repo": repo,
        "runner_id": int(registration["runner"]["id"]),
        "issued": int(now),
        "asks": 0,
        "label": label,
    }
    PEER_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_status(peer_record(name), fields)
    # Until its outbox is uploaded, its job's items may still come (writer_state).
    (OUTBOX_DIR / f"{name}.json").write_text(json.dumps({"repo": repo, "light": False}))
    log(f"PC slot {slot}: registered {name} for {repo}{f', {label}' if label else ''}")
    return f"ok {name} {label or '-'} {registration['encoded_jit_config']}"


def peer_ready(now: float) -> Counter[Want]:
    """The PC's runners sweep_peer last saw online and idle, by (repo, cache label)."""
    ready: Counter[Want] = Counter()
    for path in PEER_DIR.glob(f"{PEER_PREFIX}*.json"):
        fields = read_peer(path.stem)
        if fields and not fields.get("gone") and now - fields.get("idle_at", 0) < PEER_SEEN_SECONDS:
            ready[(str(fields["repo"]), str(fields.get("label", "")))] += 1
    return ready


def peer_status(peer: Peer, now: float) -> dict[str, Any]:
    """The PC for the status file: up (heard lately), the slots it gives, and how many of its
    runners sweep_peer last saw with a job."""
    up = now - peer.pc_heard < PEER_HEARD_SECONDS
    busy = 0
    for path in PEER_DIR.glob(f"{PEER_PREFIX}*.json"):
        fields = read_peer(path.stem)
        if fields and not fields.get("gone") and now - fields.get("busy_at", 0) < PEER_SEEN_SECONDS:
            busy += 1
    return {"up": up, "slots": peer.pc_slots if up else 0, "in_use": busy if up else 0}


def peer_drop(gh: GitHub, runner: str, fields: dict[str, Any]) -> bool:
    """Delete a PC runner's registration unless it has a job; True once it is gone."""
    if fields.get("gone"):
        return True
    if not delete_runner(gh, str(fields["repo"]), int(fields["runner_id"]), runner):
        return False
    fields["gone"] = int(time.time())
    update_peer(runner, gone=fields["gone"])
    return True


def peer_vm(
    peer: Peer, gh: GitHub, runner: str, reader: IO[bytes], writer: IO[bytes], conn: socket.socket
) -> None:
    """A PC VM's console lines, read as a local VM's are; each ask is answered on an inbox
    here and the inbox sent down: `inbox <bytes>`, then its header block and item."""
    with peer.lock:
        if old := peer.streaming.get(runner):
            with contextlib.suppress(OSError):
                old.shutdown(socket.SHUT_RDWR)
        peer.streaming[runner] = conn
    fields = read_peer(runner) or {}
    repo = str(fields.get("repo", ""))
    inbox = OUTBOX_DIR / f"{runner}.inbox"
    asks: queue.Queue[re.Match[bytes] | None] = queue.Queue(HANDOFF_MAX_ASKS)
    try:
        inbox.write_bytes(INBOX_EMPTY.ljust(512, b"\0"))

        def answered() -> None:
            with inbox.open("rb") as f:
                header = f.read(512)
                size = int(header.split(b"\0", 1)[0].split()[-1])
                writer.write(f"inbox {512 + size}\n".encode() + header)
                while size > 0 and (chunk := f.read(min(size, 1024**2))):
                    writer.write(chunk)
                    size -= len(chunk)
            writer.flush()
            with PEER_RECORDS:
                latest = read_peer(runner)
                if latest:
                    latest["asks"] = int(latest.get("asks", 0)) + 1
                    write_status(peer_record(runner), latest)

        def hand_on(ask: re.Match[bytes]) -> None:
            with contextlib.suppress(queue.Full):
                asks.put_nowait(ask)

        worker = threading.Thread(
            target=answer_asks,
            args=(gh, repo, runner, inbox, asks, int(fields.get("asks", 0)), answered),
            daemon=True,
        )
        worker.start()
        kept = sum(p.stat().st_size for p in SESSIONS_DIR.glob(f"{PEER_PREFIX}*.jsonl"))
        ours = repo.lower() == LIVE_REPO.lower() and kept < PEER_SESSIONS_MAX_BYTES
        session = SESSIONS_DIR / f"{runner}.jsonl" if ours else None
        read_console(
            reader, lambda line: write_status(PEER_DIR / f"{runner}.live", line), session, hand_on
        )
    finally:
        with contextlib.suppress(queue.Full):
            asks.put_nowait(None)
        with peer.lock:
            if peer.streaming.get(runner) is conn:
                del peer.streaming[runner]
                inbox.unlink(missing_ok=True)


def peer_live(now: float) -> list[dict[str, Any]]:
    """The PC VMs' latest live lines heard in the last PEER_LIVE_SECONDS."""
    lines = []
    for path in sorted(PEER_DIR.glob(f"{PEER_PREFIX}*.live")):
        try:
            line = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(line, dict) and now - float(line.get("heard", 0)) < PEER_LIVE_SECONDS:
            lines.append(line)
    return lines


def peer_outbox(
    peer: Peer,
    runner: str,
    size: int,
    reader: IO[bytes],
    writer: IO[bytes],
    conn: socket.socket,
) -> str:
    """An ended PC VM's outbox, written beside the Beelink's own for maybe_take_outboxes to
    file; size 0 says it left nothing, and its record goes."""
    record, tar = OUTBOX_DIR / f"{runner}.json", OUTBOX_DIR / f"{runner}.tar"
    if size == 0:
        if not tar.exists():
            record.unlink(missing_ok=True)
        return "saved"
    if not record.exists() or tar.exists():
        return "saved"  # filed already, or given up on
    with peer.lock:
        coming = sum(peer.uploading.values())
        free = shutil.disk_usage(OUTBOX_DIR).free
        if (
            runner in peer.uploading
            or size > OUTBOX_BYTES
            or free < coming + size + PEER_FREE_DISK_BYTES
        ):
            return "no"
        peer.uploading[runner] = size
    try:
        return receive_outbox(runner, size, reader, writer, conn)
    finally:
        with peer.lock:
            del peer.uploading[runner]


def receive_outbox(
    runner: str, size: int, reader: IO[bytes], writer: IO[bytes], conn: socket.socket
) -> str:
    tar = OUTBOX_DIR / f"{runner}.tar"
    part = OUTBOX_DIR / f".{runner}.part"
    part.unlink(missing_ok=True)
    conn.settimeout(120)
    writer.write(b"send\n")
    writer.flush()
    left = size
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            while left > 0 and (chunk := reader.read(min(left, 1024**2))):
                out.write(chunk)
                left -= len(chunk)
        if left:
            return "no"
        part.replace(tar)
        log(f"PC outbox of {runner} uploaded ({size} bytes)")
        return "saved"
    finally:
        part.unlink(missing_ok=True)


def read_word(path: Path) -> str:
    try:
        return path.read_text().split()[0]
    except (OSError, IndexError):
        return ""


def peer_image(peer: Peer, have: str, writer: IO[bytes], conn: socket.socket) -> str:
    """The job image (rootfs.ext4 and vmlinuz, a sparse tar through zstd) when the PC's is not the
    Beelink's. The PC checks it against `version`'s hash, which also catches a rebuild mid-copy."""
    current = read_word(IMAGES_DIR / "sha256")
    if not current or have == current:
        return "same"
    now = time.time()
    with peer.lock:
        peer.image_sent[:] = [t for t in peer.image_sent if now - t < 3600]
        if len(peer.image_sent) >= IMAGE_SENDS_PER_HOUR:
            return "busy"
    if not peer.caching.acquire(blocking=False):
        return "busy"
    try:
        with peer.lock:
            peer.image_sent.append(now)
        conn.settimeout(120)
        writer.write(b"image\n")
        writer.flush()
        pack = f"tar --sparse -C {IMAGES_DIR} -cf - rootfs.ext4 vmlinuz | zstd -1 -T2 -q -c"
        command = ["nice", "-n", "10", "ionice", "-c3", "bash", "-o", "pipefail", "-c", pack]
        # Its own process group, killed whole at the time limit, even mid-read on a slow disk.
        with subprocess.Popen(command, stdout=subprocess.PIPE, start_new_session=True) as packing:
            assert packing.stdout is not None
            limit = threading.Timer(TRANSFER_SECONDS, os.killpg, (packing.pid, signal.SIGKILL))
            limit.start()
            try:
                while chunk := packing.stdout.read(1024**2):
                    writer.write(chunk)
            finally:
                limit.cancel()
        writer.flush()
        log(f"PC image copy sent ({current[:12]})")
        return ""
    finally:
        peer.caching.release()


def peer_cache(repo: str, have: str, writer: IO[bytes]) -> str:
    """The repo's cache disk, when the PC's is not the current one."""
    version = peer_cache_version(repo)
    if not version or version == have:
        return "same"
    with cache_image(repo).open("rb") as f:
        size = os.fstat(f.fileno()).st_size
        writer.write(f"image {size} {version}\n".encode())
        shutil.copyfileobj(f, writer, 1024**2)
    writer.flush()
    return ""


def sweep_peer(gh: GitHub, peer: Peer, now: float) -> None:
    """PC runners: forget those gone an hour (and the outbox record of one that never sent
    its outbox), delete one still offline PEER_OFFLINE_SECONDS after it was registered (the
    PC is off) and an idle one older than a standby lives (a fresh registration replaces it),
    and uploads cut off midway."""
    for part in OUTBOX_DIR.glob(f".{PEER_PREFIX}*.part"):
        with contextlib.suppress(OSError):
            if now - part.stat().st_mtime > OUTBOX_WAIT_SECONDS:
                part.unlink()
    for path in PEER_DIR.glob(f"{PEER_PREFIX}*.json"):
        runner = path.stem
        fields = read_peer(runner)
        if fields is None:
            continue
        if fields.get("gone"):
            if now - float(fields["gone"]) > OUTBOX_WAIT_SECONDS:
                if not (OUTBOX_DIR / f"{runner}.tar").exists():
                    (OUTBOX_DIR / f"{runner}.json").unlink(missing_ok=True)
                (PEER_DIR / f"{runner}.live").unlink(missing_ok=True)
                path.unlink(missing_ok=True)
            continue
        try:
            runner_data = gh.request(
                "GET", f"/repos/{fields['repo']}/actions/runners/{fields['runner_id']}"
            )
        except urllib.error.HTTPError as e:
            if e.code == 404:  # its job is done and it removed itself
                update_peer(runner, gone=int(now))
            continue
        age = now - float(fields["issued"])
        offline = runner_data.get("status") != "online"
        idle = not offline and runner_data.get("busy") is False
        busy = not offline and not idle
        changes = {"idle_at": int(now) if idle else None, "busy_at": int(now) if busy else None}
        fields = update_peer(runner, **changes) or fields
        label = str(fields.get("label", ""))
        others = {w for w in peer.wants if w[0] == fields["repo"] and w[1] != label}
        if (
            (offline and age > PEER_OFFLINE_SECONDS)
            or (idle and age > STANDBY_RECYCLE_SECONDS)
            or (idle and age > PEER_RELABEL_SECONDS and others)
        ):
            peer_drop(gh, runner, fields)


# The PC side: `pc` mode, the guest's own service (homelab-ci-pc.service).


def peer_ssh() -> list[str]:
    return [
        "ssh",
        "-T",
        "-i",
        str(PEER_KEY),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=4",
        PEER_HOST_FILE.read_text().strip(),
    ]


def peer_call(line: str, timeout: float = 60) -> str:
    """One short request through the door; its reply line ("" if the door gave none). Stdin
    stays open until the reply comes: socat ends the link soon after it closes."""
    with subprocess.Popen(peer_ssh(), stdin=subprocess.PIPE, stdout=subprocess.PIPE) as link:
        assert link.stdin is not None and link.stdout is not None
        timer = threading.Timer(timeout, link.kill)
        timer.start()
        try:
            link.stdin.write(f"{line}\n".encode())
            link.stdin.flush()
            reply = link.stdout.readline(PEER_REPLY_MAX)
            link.stdin.close()
        except OSError:
            reply = b""
        finally:
            timer.cancel()
    return reply.decode("ascii", "replace").strip()


class PeerStream:
    """A PC VM's console lines and asks, sent up the door as they come; inbox answers come
    back and are written on the VM's inbox as the Beelink wrote them: the item first, the
    header last. The console reader never waits: past a full queue, lines are dropped. A
    dropped link reconnects and sends the unanswered asks again: the latest of each kind
    (handoff, capacity), so one kind never replaces the other's ask."""

    def __init__(self, runner: str, inbox: Path) -> None:
        self.runner, self.inbox = runner, inbox
        self.lines: queue.Queue[bytes | None] = queue.Queue(512)
        # Each kind's unanswered ask, and the kinds already sent on this link.
        self.asks: dict[bytes, bytes] = {}
        self.sent: set[bytes] = set()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def put(self, line: bytes) -> None:
        if line.startswith(PEER_ASKS):
            kind = line.split(b" ", 1)[0]
            self.asks[kind] = line
            self.sent.discard(kind)
            line = b""  # sent from self.asks, once per link
        with contextlib.suppress(queue.Full):
            self.lines.put_nowait(line)

    def close(self) -> None:
        self.done.set()
        with contextlib.suppress(queue.Full):
            self.lines.put_nowait(None)
        self.thread.join(timeout=10)

    def run(self) -> None:
        while not self.done.is_set():
            try:
                link = subprocess.Popen(peer_ssh(), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                assert link.stdin is not None and link.stdout is not None
                threading.Thread(target=self.answers, args=(link.stdout,), daemon=True).start()
                link.stdin.write(f"vm {self.runner}\n".encode())
                self.sent.clear()
                self.send_asks(link.stdin)
                link.stdin.flush()
                while True:
                    try:
                        line = self.lines.get(timeout=5)
                    except queue.Empty:
                        if link.poll() is not None:
                            raise OSError("the link ended") from None
                        continue
                    if line is None:
                        break
                    if line:
                        link.stdin.write(line)
                    else:
                        self.send_asks(link.stdin)
                    link.stdin.flush()
                link.stdin.close()
                link.wait(timeout=10)
                return
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                log(f"{self.runner}: link to the Beelink lost ({type(e).__name__}), again in 5 s")
                with contextlib.suppress(Exception):
                    link.kill()
                time.sleep(5)

    def send_asks(self, link: IO[bytes]) -> None:
        for kind, ask in list(self.asks.items()):
            if kind not in self.sent:
                link.write(ask)
                self.sent.add(kind)

    def answered(self, header: bytes) -> None:
        """Drop the ask this inbox header answers (its nonce is each ask's third word)."""
        nonce = header.split(b"\0", 1)[0].split()[2:3]
        for kind, ask in list(self.asks.items()):
            if ask.split()[2:3] == nonce:
                self.asks.pop(kind, None)

    def answers(self, stream: IO[bytes]) -> None:
        with contextlib.suppress(OSError, ValueError):
            while line := stream.readline(PEER_LINE_MAX):
                words = line.split()
                if len(words) != 2 or words[0] != b"inbox" or not words[1].isdigit():
                    return
                size = int(words[1])
                if not 512 <= size <= OUTBOX_BYTES:
                    return
                header = stream.read(512)
                with self.inbox.open("r+b") as f:
                    f.write(INBOX_EMPTY.ljust(512, b"\0"))
                    f.flush()
                    os.fsync(f.fileno())
                    left = size - 512
                    while left > 0 and (chunk := stream.read(min(left, 1024**2))):
                        f.write(chunk)
                        left -= len(chunk)
                    f.flush()
                    os.fsync(f.fileno())
                    f.seek(0)
                    f.write(header)
                    f.flush()
                    os.fsync(f.fileno())
                self.answered(header)


def pc_slot_count() -> int:
    """The slots the PC gives now, set by the keeper: 0 (gaming, off) up to the slots
    installed; 0 if unreadable or written before the guest booted (the keeper has not
    spoken since: Colin may be gaming)."""
    try:
        text = PC_SLOTS_FILE.read_text().strip()
        booted = time.time() - float(Path("/proc/uptime").read_text().split()[0])
        if PC_SLOTS_FILE.stat().st_mtime < booted:
            return 0
    except (OSError, ValueError):
        return 0
    return min(int(text), slot_count()) if text.isdecimal() else 0


def pc_cache_image(named: str) -> Path:
    """The PC's copy of a repo's cache disk, by the repo's lowercase name."""
    return CACHE_DIR / f"{named.replace('/', '__')}.ext4"


def pc_cache_version(named: str) -> str:
    """The version of the cache disk copy the PC holds, "-" for none."""
    try:
        return pc_cache_image(named).with_suffix(".version").read_text().strip() or "-"
    except OSError:
        return "-"


def start_pc_vm(repo: str, slot: int, wanted: set[str]) -> bool:
    """Register a ready runner for the slot through the door and boot its VM, with the cache
    disk its label names if the PC holds a copy (else that copy is fetched for next time)."""
    reply = peer_call(f"register {slot} {repo}").split()
    if len(reply) != 4 or reply[0] != "ok" or not PEER_RUNNER.fullmatch(reply[1]):
        log(f"PC slot {slot}: not registered ({' '.join(reply[:1]) or 'no reply'})")
        return False
    _, name, label, jit = reply
    metadata = STATE_DIR / f"metadata-slot{slot}.json"
    fd = os.open(metadata, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"task": "runner", "jitconfig": jit}, f)
    named = cache_label_repo(label) or ""
    image = pc_cache_image(named) if named else None
    cache = image if image and image.exists() else None
    if named:
        wanted.add(named)
    run = Launch(slot, repo, 0, name, time.time(), metadata, standby=True, label=label)
    (OUTBOX_DIR / f"{name}.json").write_text(json.dumps({"repo": repo}))
    env = {
        "VCPUS": str(PC_SIZE[0]),
        "MEM_MIB": str(PC_SIZE[1]),
        "CACHE_IMAGE": str(cache or ""),
        "CACHE_OUT": str(OUTBOX_DIR / f"{name}.tar"),
        "INBOX": str(OUTBOX_DIR / f"{name}.inbox"),
    }
    start_vm(run, STANDBY_TIMEOUT_SECONDS, env, peer=True)
    log(f"PC slot {slot}: ready runner {name} started")
    return True


def outbox_end(tar: Path) -> int:
    """How much of a sparse outbox to send: up to its first hole, and only handoffs (the
    Beelink makes cache disks from its own VMs alone)."""
    try:
        with tar.open("rb") as f:
            if not HANDOFF_HEADER.match(f.read(512)):
                return 0
            return min(os.lseek(f.fileno(), 0, os.SEEK_HOLE), OUTBOX_BYTES)
    except OSError:
        return 0


def ship_outboxes(running: set[str], tried: dict[str, float], now: float) -> None:
    """Send each ended PC VM's outbox up the door, once; a refusal is tried again later."""
    for record in OUTBOX_DIR.glob(f"{PEER_PREFIX}*.json"):
        runner = record.stem
        if f"{UNIT_PREFIX}{runner}" in running or now - tried.get(runner, 0) < OUTBOX_RETRY_SECONDS:
            continue
        tar = record.with_suffix(".tar")
        # run-vm.sh makes the outbox as the VM starts: until then the VM may be on its way.
        if not tar.exists() and now - record.stat().st_mtime < 600:
            continue
        tried[runner] = now
        size = outbox_end(tar)
        try:
            with subprocess.Popen(
                peer_ssh(), stdin=subprocess.PIPE, stdout=subprocess.PIPE
            ) as link:
                assert link.stdin is not None and link.stdout is not None
                link.stdin.write(f"outbox {runner} {size}\n".encode())
                link.stdin.flush()
                reply = link.stdout.readline(PEER_LINE_MAX).strip()
                if reply == b"send":
                    with tar.open("rb") as f:
                        left = size
                        while left > 0 and (chunk := f.read(min(left, 1024**2))):
                            link.stdin.write(chunk)
                            left -= len(chunk)
                    link.stdin.flush()
                    reply = link.stdout.readline(PEER_LINE_MAX).strip()
                link.stdin.close()
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            log(f"outbox of {runner}: {type(e).__name__}: {e}")
            continue
        if reply == b"saved":
            tar.unlink(missing_ok=True)
            record.unlink(missing_ok=True)
            tried.pop(runner, None)


def refresh_cache(named: str) -> None:
    """Fetch the Beelink's cache disk for the repo (lowercase name) when newer than the PC's."""
    image = pc_cache_image(named)
    version_file, have = image.with_suffix(".version"), pc_cache_version(named)
    staged = image.with_suffix(".part")
    with subprocess.Popen(peer_ssh(), stdin=subprocess.PIPE, stdout=subprocess.PIPE) as link:
        assert link.stdin is not None and link.stdout is not None
        link.stdin.write(f"cache {named} {have}\n".encode())
        link.stdin.flush()
        words = link.stdout.readline(PEER_LINE_MAX).split()
        if len(words) != 3 or words[0] != b"image" or not words[1].isdigit():
            return
        size, version = int(words[1]), words[2].decode("ascii")
        left = size
        with staged.open("wb") as out:
            while left > 0 and (chunk := link.stdout.read(min(left, 1024**2))):
                out.write(chunk)
                left -= len(chunk)
    if left:
        staged.unlink(missing_ok=True)
        return
    staged.replace(image)  # VMs already booted keep the disk they opened
    version_file.write_text(f"{version}\n")
    log(f"cache for {named}: fetched {version} ({size} bytes)")


def peer_version() -> int:
    """`peer-version`: the commit and image hash the Beelink runs, for update.sh --follow."""
    words = peer_call("version").split()
    if len(words) != 2 or not re.fullmatch(r"[0-9a-f]{40}|-", words[0]):
        return 1
    print(*words)
    return 0


def fetch_image(want: str) -> int:
    """`fetch-image <sha256>`: copy the Beelink's job image, and switch to it only if it is the
    one `want` names. Staged beside the images (one filesystem), both files are renamed into place
    together; running VMs keep the copies they opened."""
    if not SHA256.fullmatch(want):
        return 2
    IMAGES_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    if shutil.disk_usage(IMAGES_DIR).free < 12 * 1024**3:
        log("job image not fetched: under 12 GiB free")
        return 1
    staging = IMAGES_DIR / ".incoming"
    subprocess.run(["rm", "-rf", "--one-file-system", str(staging)], check=False)
    staging.mkdir(mode=0o700)
    try:
        have = read_word(IMAGES_DIR / "sha256") or "-"
        with subprocess.Popen(peer_ssh(), stdin=subprocess.PIPE, stdout=subprocess.PIPE) as link:
            assert link.stdin is not None and link.stdout is not None
            link.stdin.write(f"image {have}\n".encode())
            link.stdin.flush()
            said = link.stdout.readline(PEER_LINE_MAX).strip()
            if said != b"image":
                why = said.decode("ascii", "replace") or "nothing"
                log(f"job image not fetched: the Beelink said {why}")
                return 0 if said == b"same" else 1
            unpack = f"zstd -d -q -c | tar --sparse -x -C {staging}"
            with subprocess.Popen(
                ["bash", "-o", "pipefail", "-c", unpack], stdin=subprocess.PIPE
            ) as unpacking:
                assert unpacking.stdin is not None
                shutil.copyfileobj(link.stdout, unpacking.stdin, 1024**2)
                unpacking.stdin.close()
            link.stdin.close()
        got = subprocess.run(
            "sha256sum rootfs.ext4 vmlinuz | sha256sum | cut -c1-64",
            shell=True,
            cwd=staging,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if got != want:
            log(f"job image not switched: it hashes to {got[:12] or 'nothing'}, not {want[:12]}")
            return 1
        for name in ("rootfs.ext4", "vmlinuz"):
            (staging / name).chmod(0o644)
            (staging / name).replace(IMAGES_DIR / name)
        (IMAGES_DIR / "sha256").write_text(f"{want}\n")
        log(f"job image switched to the Beelink's ({want[:12]})")
        return 0
    finally:
        subprocess.run(["rm", "-rf", "--one-file-system", str(staging)], check=False)


def fetch_caches(wanted: set[str]) -> None:
    """Each cache disk copy the PC's runners have wanted, one after another."""
    for named in sorted(wanted):
        try:
            refresh_cache(named)
        except Exception as e:  # its VMs run without the cache disk until the next try
            log(f"cache for {named}: not fetched, {type(e).__name__}: {e}")


def pc_main(argv: list[str]) -> int:
    """`pc`: the PC guest's service. Keeps a ready runner per free slot, stops runners on
    slots the count no longer has (at once if idle, else after PC_DRAIN_SECONDS), and ships
    ended VMs' outboxes. Holds no GitHub key."""
    argparse.ArgumentParser(prog="ci_supervisor.py pc").parse_args(argv)
    repo = LIVE_REPO
    for folder in (STATE_DIR, OUTBOX_DIR, CACHE_DIR):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    over: dict[str, float] = {}
    released: dict[str, float] = {}
    tried: dict[str, float] = {}
    slot_paused: dict[int, float] = {}
    cache_checked = said_at = 0.0
    said_slots = -1
    fetching = shipping = threading.Thread()
    wanted = {p.stem.replace("__", "/", 1) for p in CACHE_DIR.glob("*.ext4")}
    log("PC slots: keeping ready runners")
    while True:
        now = time.time()
        try:
            running = running_vms()
            if running is None:
                raise RuntimeError("cannot list the running VMs")
            slots = pc_slot_count()
            by_slot = {}
            for unit in running:
                if match := PEER_RUNNER.fullmatch(unit.removeprefix(UNIT_PREFIX)):
                    by_slot[int(match[1])] = match[0]
            for slot, runner in by_slot.items():
                if slot < slots:
                    over.pop(runner, None)
                    continue
                since = over.setdefault(runner, now)
                # A busy runner's release is refused: asked again a minute later, not every poll.
                ask = now - released.get(runner, 0) >= 60
                if ask:
                    released[runner] = now
                gone = ask and peer_call(f"release {runner}") == "gone"
                if gone or now - since > PC_DRAIN_SECONDS:
                    log(f"PC slot {slot}: {runner} stopped, the PC gives {slots} slots")
                    subprocess.run(
                        ["systemctl", "stop", "--no-block", f"{UNIT_PREFIX}{runner}"], check=False
                    )
            # The Beelink shows the PC's slots on the Control room, paused ones included.
            due = slots != said_slots or now - said_at >= PC_SAY_SECONDS
            if due and peer_call(f"slots {slots}") == "ok":
                said_slots, said_at = slots, now
            # Uploads in the background: a 6 GiB one must not hold up a gaming drain.
            if not shipping.is_alive():
                shipping = threading.Thread(
                    target=ship_outboxes, args=(running, tried, now), daemon=True
                )
                shipping.start()
            # Never while gaming: the copies cross Windows' network.
            fresh = all(pc_cache_image(n).exists() for n in wanted)
            due = now - cache_checked > (PC_CACHE_EVERY if fresh else 60)
            if slots and due and wanted and not fetching.is_alive():
                cache_checked = now
                fetching = threading.Thread(target=fetch_caches, args=(set(wanted),), daemon=True)
                fetching.start()
            for slot in range(slots):
                room = mem_available_mib() - memory_reserve_mib()
                if slot in by_slot or now < slot_paused.get(slot, 0) or room < PC_SIZE[1] + 256:
                    continue
                if not start_pc_vm(repo, slot, wanted):
                    slot_paused[slot] = now + 30
        except Exception as e:
            log(f"PC poll failed: {type(e).__name__}: {e}")
        time.sleep(PC_POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["vm"]:
        return vm_main(argv[1:])
    if argv[:1] == ["pc"]:
        return pc_main(argv[1:])
    if argv == ["peer-version"]:
        return peer_version()
    if argv[:1] == ["fetch-image"] and len(argv) == 2:
        return fetch_image(argv[1])
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
    # Root's alone, like the outboxes: private repos' sessions and changes.
    HANDOFF_DIR.mkdir(mode=0o700, exist_ok=True)
    HANDOFF_DIR.chmod(0o700)
    swept = 0.0
    # Half-built caches from a supervisor that was killed mid-build.
    for leftover in CACHE_DIR.glob("tmp*"):
        subprocess.run(["rm", "-rf", "--one-file-system", str(leftover)], check=False)
    state = State(slots=slot_count(), light_slots=light_slot_count())
    write_memory_metrics()
    # The standby waits where the last job before the restart ran, not on the first repo
    # listed (4 Nov: it booted for a repo with no work, for 45 minutes).
    state.last_repo, state.last_label = recalled_last_job()
    if state.last_repo:
        state.used.add(state.last_repo)
    state.last_light_repo = recalled_repo(LAST_LIGHT_REPO_FILE, "")
    state.last_deploy_repo = recalled_repo(LAST_DEPLOY_REPO_FILE, DEFAULT_DEPLOY_REPO)
    state.last_small_repo = recalled_repo(LAST_SMALL_REPO_FILE, DEFAULT_DEPLOY_REPO)
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
    global PEER
    peer = PEER = Peer(gh._token) if peer_slots() and peer_door_installed() else None
    if peer:
        threading.Thread(target=serve_peer, args=(peer,), daemon=True).start()
    peer_swept = 0.0
    peer_sweeper = threading.Thread()
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
                if peer:
                    # The PC runs Auto Kaizen's builds only: no other repo's jobs, items or caches.
                    peer.served = frozenset(r for r in repos if r.lower() == LIVE_REPO.lower())
                    peer.caches = frozenset(r for r in repos if cache_image(r).exists())
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
            write_status(LIVE_FILE, live(state, time.time()))
            prune_sessions(SESSIONS_DIR, time.time())
            if time.time() - swept > HANDOFF_SWEEP_SECONDS:
                swept = time.time()
                sweep_handoffs(swept)
            if (
                peer
                and time.time() - peer_swept > PEER_SWEEP_SECONDS
                and not peer_sweeper.is_alive()
            ):
                peer_swept = time.time()
                peer_sweeper = threading.Thread(
                    target=sweep_peer, args=(gh.clone(), peer, peer_swept), daemon=True
                )
                peer_sweeper.start()
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
