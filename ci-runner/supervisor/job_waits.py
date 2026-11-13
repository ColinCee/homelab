"""Why each queued job has not started, and for how long: the wait log, the waits file a job's
line goes to when it stops waiting, and the wait metrics Prometheus alerts on."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import telemetry

if TYPE_CHECKING:
    from ci_supervisor import State

log = logging.getLogger("ci-supervisor").info


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


# Each job that waited, one JSON line when it stops waiting, with its seconds per kind of
# wait (WaitKind) and the reason of its longest kind, for speed reports. Lines older
# than WAITS_KEEP_SECONDS are dropped at most every WAITS_PRUNE_SECONDS. "unseen" is the
# time from GitHub queueing the job to the supervisor first seeing it (a restart counts
# again from GitHub's queueing).
WAITS_FILE = Path("/var/lib/homelab-ci/status/waits.jsonl")
WAITS_KEEP_SECONDS = 30 * 24 * 3600
WAITS_PRUNE_SECONDS = 3600.0
# The longest-waiting queued job's seconds so far per kind of wait, for Prometheus (Alloy's
# textfile collector), at most every WAIT_METRICS_SECONDS: a write every 2 s poll would only
# add load to the shared disk.
WAIT_METRICS = Path("/var/lib/homelab-metrics/ci-waits.prom")
WAIT_METRICS_SECONDS = 15.0
# A VM is told how long each queued job of its repo and label had waited here, and why
# (the wait log's reason), at most this many, oldest first: ci-boot writes it to
# /etc/homelab-ci/waits.json for the job's own record.
VM_WAITS_MAX = 50


class WaitKind(StrEnum):
    """What a queued job waits for: the waits file's columns and the wait metric's `kind`. UNSEEN
    is GitHub queueing it before the supervisor first saw it."""

    MEMORY = "memory"
    DISK = "disk"
    SLOTS = "slots"
    BOOTING = "booting"
    READY = "ready"
    UNSEEN = "unseen"
    OTHER = "other"


class Reason(NamedTuple):
    """Why a queued job has not started: its repo, the words the wait log gives, and its kind."""

    repo: str
    why: str
    kind: WaitKind = WaitKind.OTHER


@dataclass
class Wait:
    repo: str
    since: float
    # When GitHub queued the job, or `since` when unknown.
    queued: float = 0.0
    logged: float | None = None
    # The kind of wait at the last poll that saw it, and when: the time until the next
    # poll counts for that kind.
    kind: WaitKind = WaitKind.OTHER
    seen: float = 0.0
    seconds: dict[WaitKind, float] = field(default_factory=dict)
    # The reason the wait log gives at the last poll that saw it, and the latest one for
    # each kind, so the finished record names the reason of its longest kind.
    why: str = ""
    whys: dict[WaitKind, str] = field(default_factory=dict)
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
    reasons: Mapping[int, Reason],
    now: float,
    queued_at: Mapping[int, float] | None = None,
    workflows: Mapping[int, str] | None = None,
    unmatched: Mapping[int, tuple[str, ...]] | None = None,
) -> None:
    """Log each queued job not started, with why and how long it has waited here: when it
    starts waiting and every WAIT_LOG_SECONDS, and at once if GitHub had queued it
    UNSEEN_LOG_SECONDS before the supervisor saw it. A job that was logged gets one more line
    when it stops waiting. Jobs of repos not polled this time are left as they are."""
    queued_at = queued_at or dict[int, float]()
    workflows = workflows or dict[int, str]()
    unmatched = unmatched or dict[int, tuple[str, ...]]()
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
    for job_id, (repo, why, kind) in sorted(reasons.items()):
        wait = state.waits.get(job_id)
        unseen = 0.0
        if wait is None:
            queued = min(queued_at.get(job_id) or now, now)
            wait = state.waits[job_id] = Wait(
                repo, now, queued, seen=now, workflow=workflows.get(job_id, "")
            )
            unseen = wait.seconds[WaitKind.UNSEEN] = now - queued
        wait.count(now)
        wait.kind = kind
        wait.why = wait.whys[kind] = why
        wait.unmatched = unmatched.get(job_id, ())
        waited = now - wait.since
        late = unseen >= UNSEEN_LOG_SECONDS
        quiet = (
            kind in (WaitKind.BOOTING, WaitKind.READY) and waited < WAIT_LOG_SECONDS and not late
        )
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
    names = {kind: f'kind="{kind}"' for kind in WaitKind}
    if stray:
        labels = telemetry.label(",".join(stray.unmatched))
        names[WaitKind.OTHER] += f',repo="{telemetry.label(stray.repo)}",labels="{labels}"'
    lines = [
        f"homelab_ci_wait_seconds{{{names[kind]}}} {round(seconds.get(kind, 0.0))}"
        for kind in WaitKind
    ]
    queued: dict[tuple[str, str], float] = {}
    for wait in state.waits.values():
        key = (wait.repo, wait.workflow)
        queued[key] = max(queued.get(key, 0.0), now - wait.queued)
    lines += [
        f'homelab_ci_queued_seconds{{repo="{telemetry.label(repo)}",workflow="{telemetry.label(name)}"}}'
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
        telemetry.replace_file(WAIT_METRICS, "\n".join(lines) + "\n")
        state.wait_metrics_written = now
        state.wait_metrics_error = ""
    except OSError as e:
        # Its strerror, not the message, which names a new temporary file each time.
        why = f"{type(e).__name__}: {e.strerror or e}"
        if why != state.wait_metrics_error:
            log(f"could not write {WAIT_METRICS}: {why}")
        state.wait_metrics_error = why


def note_poll(state: State, ok: bool, now: float) -> None:
    """Counts failed polls in a row, for Prometheus: no VM starts while every poll fails. The
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


def rest(state: State, seconds: float) -> None:
    """Sleep between polls, rewriting the wait metrics when due: a rate limit holds the poll
    for up to an hour, and a file left unwritten that long reads as a stopped supervisor
    rather than as polls failing."""
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        time.sleep(min(left, WAIT_METRICS_SECONDS))
        if time.time() - state.wait_metrics_written >= WAIT_METRICS_SECONDS:
            write_wait_metrics(state, time.time())


def record_wait(state: State, job_id: int, wait: Wait, now: float) -> None:
    """Append the job's wait to WAITS_FILE, pruning old lines first when due. A failure is
    logged once and never stops the poll."""
    line = {"job": job_id, "repo": wait.repo, "since": int(wait.since), "until": int(now)}
    line |= {kind: round(wait.seconds.get(kind, 0.0)) for kind in WaitKind}
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
        why = f"{type(e).__name__}: {e.strerror or e}"
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
        except ValueError, KeyError, TypeError:
            continue  # a half-written line from a crash
    telemetry.replace_file(WAITS_FILE, "".join(kept))
