"""Start a throwaway Firecracker VM for each queued CI job on Colin's served repos.

Polls GitHub for queued jobs asking for the `homelab-fc` label, on every private
repository of Colin's that its token can see (a repository signs up by being added to
the token's repository access in GitHub, nothing on the host) and on the public ones in
github_api.PUBLIC_REPOS, whose VMs take only Colin's own jobs. For each job it asks
GitHub for a just-in-time runner registration (good for exactly one job) and boots a
VM with run_vm.py, which receives that registration through Firecracker's metadata
service. The GitHub token stays on
the host; a VM only ever sees its one-job registration. A repository that is not
Colin's, or public and not listed, is never served. See docs/architecture.md, "CI runner".

Installed root-owned by ci-runner/install_host.py and run by homelab-ci-supervisor.service.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cache_disks
import github_api
import job_waits
import load_testing
import telemetry
import vms
from github_api import GitHub, GitHubApi, Job, RateLimited, Serving, Want
from job_waits import Reason, Wait, WaitKind
from vms import Launch

log = logging.getLogger("ci-supervisor").info


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


TOKEN_FILE = Path("/etc/homelab-ci/github-token")


# Unchanged answers are free conditional requests, so polling often costs nothing.
POLL_SECONDS = 2


# How many build VMs wait ready at once (each about 450 MB while idle), from
# /etc/homelab-ci/standbys if present (see standby_count and standby_wants).
STANDBY_COUNT_FILE = Path("/etc/homelab-ci/standbys")


# What the CI runner is doing: rewritten after every poll, readable by anyone on the host.
# Nothing secret goes in it (see status()).
STATUS_FILE = Path("/var/lib/homelab-ci/status/machines.json")


DISK_WAIT_LIMIT = 50.0
DISK_PACE_SECONDS = 60.0


# A new run's first build job waits behind later jobs of started runs at most this long
# since GitHub queued it, then goes back to oldest first: a few minutes, never starved.
FIRST_BUILD_WAIT_MAX = 600.0


@dataclass
class State:
    slots: int = load_testing.DEFAULT_SLOTS
    launches: dict[int, Launch] = field(default_factory=dict)
    serving: dict[str, Serving] = field(default_factory=dict)
    cache_checked: dict[str, float] = field(default_factory=dict)
    cache_busy: threading.Lock = field(default_factory=threading.Lock)
    # The threads that ask the polled repos for queued jobs at once, kept so each keeps
    # its open connection to GitHub.
    askers: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(ASK_THREADS, thread_name_prefix="ask")
    )
    # The repository and cache label of the last build job a VM started for: the standby
    # boots for the same, so the next such job of the run takes it.
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


def start_order(jobs: Iterable[Job], waited_since: Callable[[int], float], now: float) -> list[Job]:
    """Jobs in the order they get slots and memory: oldest first, except that a new run's
    first build job waits behind later jobs of runs already started, so a run half done is
    not left behind newer runs. A job queued over FIRST_BUILD_WAIT_MAX goes back to oldest
    first."""

    def waited(job: Job) -> float:
        return now - (job.queued_at or waited_since(job.id))

    def new(job: Job) -> bool:
        return not job.run_started and waited(job) < FIRST_BUILD_WAIT_MAX

    return sorted(jobs, key=lambda job: (new(job), job.id))


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


def ask(gh: GitHubApi, state: State, repo: str, now: float) -> tuple[list[Job] | None, float]:
    """One repo's queued jobs (None if it is not served this poll), and the seconds the
    asking took. Run for every polled repo at once, each on its own thread."""
    asked = time.monotonic()
    # A repo that is no longer served has nothing queued as far as we are
    # concerned, so its idle VMs are stopped and no new ones start. One
    # repo GitHub will not show is skipped this poll; it never stops the others
    # (3 Nov: a repo the token could not see stopped all CI).
    try:
        if not github_api.check_served(gh, state, repo, now):
            github_api.skipped(repo, None)
            return None, time.monotonic() - asked
        jobs = github_api.find_queued(gh, repo, colins_only=github_api.is_public(state, repo))
        # A repo about to get its first VM is asked about its serving directly.
        if (
            jobs
            and repo not in state.used
            and not (
                github_api.check_served(gh, state, repo, now, fresh=True)
                and github_api.only_colin(gh, repo)
            )
        ):
            return None, time.monotonic() - asked
        github_api.skipped(repo, None)
        return jobs, time.monotonic() - asked
    except RateLimited:
        raise
    except Exception as e:
        github_api.skipped(repo, e)
        return None, time.monotonic() - asked


def tick(gh: GitHubApi, state: State, repos: list[str], now: float) -> dict[str, float]:
    """One poll. Returns how long asking each repo for queued jobs took, in seconds."""
    queued: Counter[Want] = Counter()
    job_ids: dict[Want, list[int]] = {}
    served: list[str] = []
    spent: dict[str, float] = {}
    # Why each queued job not started this poll waits: (repo, why).
    reasons: dict[int, Reason] = {}
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
        served.append(repo)
        for job in jobs:
            queued_at[job.id] = job.queued_at
            workflows[job.id] = job.workflow
            if not job.ours:
                reasons[job.id] = Reason(repo, f"no VM has its labels ({', '.join(job.labels)})")
                unmatched[job.id] = job.labels
                continue
            queued[job.want] += 1
            job_ids.setdefault(job.want, []).append(job.id)
            polled.append(job)
    vms.reap(gh, state, now, queued, served)
    private = [r for r in served if not github_api.is_public(state, r)]
    # Ended VMs' outboxes are after-the-fact too: a failure to list them never stops a start.
    try:
        cache_disks.maybe_take_outboxes(gh, state, now)
    except RateLimited:
        raise
    except Exception as e:
        log(f"could not look at ended VMs' outboxes: {type(e).__name__}: {e}")
    # A VM still on its way to a job, or a standby waiting for one, covers a queued job
    # with the same repo and cache label (or none).
    # Those whose runner is up and idle are ready: the rest of the wait is GitHub's.
    ways = [
        run for run in state.launches.values() if vms.on_its_way(gh, run, now, queued[run.want])
    ]
    booting: Counter[Want] = Counter(run.want for run in ways)
    ready: Counter[Want] = Counter(run.want for run in ways if run.ready)
    draining = load_testing.DRAIN_FILE.exists()
    if state.draining and not draining:
        # A failed load test lowers the count while no VM runs; never raised here, as more
        # slots need install_host.py's users and taps.
        state.slots = min(state.slots, load_testing.slot_count())
    state.draining = draining
    free = [slot for slot in range(state.slots) if slot not in state.launches and not draining]
    # GitHub's job ids only grow, so the smallest is the oldest.
    order = start_order(polled, lambda i: job_waits.waited_since(state, i, now), now)
    rank = {(job.id, job.want): n for n, job in enumerate(order)}
    in_order = [job.want for job in order]
    starts = plan(in_order, booting, len(free))
    # The VMs started this poll are counted by their size: they have touched almost
    # nothing yet, so MemAvailable does not show them.
    reserve = vms.memory_reserve_mib()
    room = vms.mem_available_mib() - reserve
    disk_wait = vms.disk_wait_percent()
    disk_paced = disk_wait > DISK_WAIT_LIMIT / 2
    disk_busy = disk_wait > DISK_WAIT_LIMIT or (disk_paced and now < state.disk_paced_until)
    if disk_busy:
        room = min(room, 0)

    def why(repo: str, need: int) -> Reason:
        """Why a job needing `need` MiB is held: the disk or memory."""
        if disk_busy or disk_paced:
            busy = f"the disk (tasks waited on it {disk_wait:.0f}% of the last minute)"
            return Reason(repo, busy, WaitKind.DISK)
        free = (
            f"memory ({max(room, 0)} MiB free above the {reserve} MiB reserve, a VM needs {need})"
        )
        return Reason(repo, free, WaitKind.MEMORY)

    def pace() -> None:
        """One VM a minute while the disk is half busy: no more room after this start."""
        nonlocal room
        if disk_paced:
            state.disk_paced_until = now + DISK_PACE_SECONDS
            room = min(room, 0)

    # In start order (a started run's job first, oldest first within each group, a new build
    # back in after FIRST_BUILD_WAIT_MAX); once one is held for memory, those after it wait too.
    builds = with_job_ids(starts, job_ids, booting, rank)
    held: dict[int, Reason] = {}
    first_held_build: int | None = None
    started = 0
    for job_id, (repo, label) in sorted(builds, key=rank.__getitem__):
        mem = vms.next_size(state, False)[1]
        if first_held_build is not None or mem > room:
            if first_held_build is None:
                first_held_build = job_id
            held[job_id] = why(repo, mem)
            continue
        vms.launch(
            gh,
            state,
            repo,
            free[started],
            now,
            label=label,
            cache=cache_disks.cache_for(repo, label, private),
            jobs=job_ids[(repo, label)],
            load_test=load_testing.read_load_test() or {},
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
                reasons[job_id] = Reason(want[0], "the host's load test")
            elif job_id in held:
                reasons[job_id] = held[job_id]
            elif n < ready[want]:
                reasons[job_id] = Reason(want[0], job_waits.READY, WaitKind.READY)
            elif n < booting[want] or job_id in planned:
                reasons[job_id] = Reason(want[0], job_waits.BOOTING, WaitKind.BOOTING)
            else:
                reasons[job_id] = Reason(want[0], full, WaitKind.SLOTS)
    job_waits.note_waits(state, served, reasons, now, queued_at, workflows, unmatched)
    free = free[started:]
    # A build job still waiting for a slot, or for memory, gets the standby's slot and
    # memory, if the standby cannot take it.
    waiting = queued - booting
    waiting.subtract(launched)
    for run in list(state.launches.values()):
        if run.standby and any(n > 0 and want != run.want for want, n in waiting.items()):
            vms.stop_if_still_idle(gh, run, "is in the way of a queued job")
    if draining:
        for run in [r for r in state.launches.values() if r.standby]:
            vms.stop_if_still_idle(gh, run, "gives way to the host's load test")
        return spent
    standbys = [r for r in state.launches.values() if r.standby]
    # A standby is the first thing given up: never booted while a job waits for memory, nor
    # while a build job waits for a slot (one at a time while free slots outnumber them).
    waiting_builds = sum(n for n in waiting.values() if n > 0)
    # Each want gets one first; once all have one, a further standby waits for the most needed,
    # so two runs of the busiest repo starting jobs together both find one booted.
    wants = standby_wants(state, repos, served)
    uncovered = next((w for w in wants if all(r.want != w for r in standbys)), None)
    target = uncovered or (wants[0] if wants else None)
    if uncovered and len(standbys) >= standby_count():
        # A second standby for one want gives its place to a want with none.
        seen: set[Want] = set()
        for run in sorted(standbys, key=lambda r: r.started):
            if run.want in seen:
                vms.stop_if_still_idle(gh, run, "gives its place to a standby for another job")
                break
            seen.add(run.want)
    if (
        target
        and not state.held
        and len(free) > waiting_builds
        and len(standbys) < standby_count()
        and vms.next_size(state, True)[1] <= room
    ):
        repo, label = target
        cache = cache_disks.cache_for(repo, label, private)
        load_test = load_testing.read_load_test() or {}
        vms.launch(
            gh,
            state,
            repo,
            free[0],
            now,
            label=label,
            cache=cache,
            standby=True,
            load_test=load_test,
        )
        room -= vms.next_size(state, True)[1]
        pace()
    return spent


def with_job_ids(
    starts: list[Want],
    job_ids: Mapping[Want, list[int]],
    covered: Mapping[Want, int],
    rank: Mapping[tuple[int, Want], int],
) -> list[tuple[int, Want]]:
    """Each planned start with the id of the job it is for: a want's first jobs in start
    order not covered by a VM already on its way, so a started run's job sharing a want with
    an older job gets memory at its own place in start order."""
    taken = Counter(covered)
    paired: list[tuple[int, Want]] = []
    for want in starts:
        ids = sorted(job_ids[want], key=lambda i: rank[(i, want)])
        paired.append((ids[taken[want]], want))
        taken[want] += 1
    return paired


def hold_for_memory(state: State, repos: list[str], held: Mapping[int, Reason]) -> None:
    """Note the queued jobs left waiting for memory or the disk (job id: its repo and why).
    They stay queued and are tried again next poll; note_waits logs them. Jobs of repos not
    polled this time keep their mark."""
    state.held = {i: r for i, r in state.held.items() if r not in repos} | {
        i: reason.repo for i, reason in held.items()
    }


def standby_wants(state: State, repos: list[str], served: list[str]) -> list[Want]:
    """The repos and cache labels build standbys boot for, most needed first: the last build
    job's (or the first polled repo's) once no VM runs it, then each running build job's,
    oldest first (a run's next job asks for the one before it seconds after it ends, even when
    another run's job started in between). Booted for the cache label too, else a job
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
    return [w for w in dict.fromkeys(wants) if w[0] in served]


def standby_count() -> int:
    """How many build standbys may wait (see standby_wants for which): 2 unless
    /etc/homelab-ci/standbys says otherwise."""
    return count_in(STANDBY_COUNT_FILE, default=2)


def count_in(path: Path, default: int = 1) -> int:
    """How many standbys a host file asks for: `default` if it is missing or not a number."""
    try:
        return max(0, int(path.read_text().strip()))
    except OSError, ValueError:
        return default


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
            image.stem.replace("__", "/", 1) for image in cache_disks.CACHE_DIR.glob("*__*.ext4")
        ),
        "github_ok": github_ok,
        "mem_available_mib": mem_available,
        "held_for_memory": len(state.held),
        "queued": len(state.waits),
        "in_use_repos": dict(Counter(run.repo for run in runs if not run.standby)),
        "draining": state.draining,
        "load_test": load_testing.read_load_test(),
    }


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
    busy = {r for r in repos if r in state.used or cache_disks.cache_image(r).exists()}
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
        return vms.vm_main(argv[1:])
    if argv == ["start-load-test-if-due"]:
        return load_testing.start_load_test_if_due()
    if argv == ["load-test"]:
        return load_testing.run_load_test()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="one poll, for testing")
    args = parser.parse_args(argv)
    gh = GitHub(telemetry.secret(TOKEN_FILE))
    repos: list[str] = []
    listed = 0.0
    vms.STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    cache_disks.CACHE_DIR.mkdir(mode=0o755, exist_ok=True)
    cache_disks.OUTBOX_DIR.mkdir(mode=0o700, exist_ok=True)
    # Half-built caches from a supervisor that was killed mid-build.
    for leftover in cache_disks.CACHE_DIR.glob("tmp*"):
        subprocess.run(["rm", "-rf", "--one-file-system", str(leftover)], check=False)
    state = State(slots=load_testing.slot_count())
    vms.write_memory_metrics()
    # The standby waits where the last job before the restart ran, not on the first repo
    # listed (4 Nov: it booted for a repo with no work, for 45 minutes).
    state.last_repo, state.last_label = vms.recalled_last_job()
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
    public = sorted(github_api.PUBLIC_REPOS)
    log(f"{state.slots} slots; serving private repos the token can see, and {public}")
    # VMs started before a restart are still running: take them back before anything else.
    vms.adopt(state)
    while True:
        wait: float = POLL_SECONDS
        github_ok = False
        # Any failure is logged and retried; exiting would kill the running jobs.
        try:
            if listing is None and time.time() - listed >= github_api.REPOS_EVERY:
                listed = time.time()
                listing = lister.submit(github_api.list_repos, list_gh)
            # The first listing is waited for: there is nothing to serve before it.
            if listing and (listing.done() or not repos):
                done, listing = listing, None
                # A failed listing keeps the last list (none at start), never ending the poll.
                try:
                    served, found = done.result()
                    # Listed as served just now: quiet repos need no request of their own.
                    for repo, branch in served.items():
                        state.serving[repo] = Serving(
                            True, listed, branch, repo in github_api.PUBLIC_REPOS
                        )
                    github_api.skipped("the repo list", None)
                except RateLimited:
                    raise
                except Exception as e:
                    github_api.skipped("the repo list", e)
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
                unclean = github_api.remove_stale_runners(gh, unclean, running)
            now = time.time()
            polling = due(standby_first(repos, state.last_repo), state, now)
            spent = tick(gh, state, polling, now)
            for repo in polling:
                state.polled[repo] = now
            if (took := time.monotonic() - started) >= SLOW_POLL_SECONDS:
                log(slow_poll(took, gh.requests - asked, gh.seconds - waited, spent))
            github_ok = True
            job_waits.note_poll(state, True, time.time())
        except RateLimited as e:
            log(str(e))
            job_waits.note_poll(state, False, time.time())
            wait = max(POLL_SECONDS, e.until - time.time() + 1)
        except Exception as e:
            log(f"poll failed: {type(e).__name__}: {e}")
            job_waits.note_poll(state, False, time.time())
            # Without GitHub nothing re-checks serving, so no VM is left waiting meanwhile.
            if state.poll_failures == 5:
                for run in [r for r in state.launches.values() if r.standby]:
                    vms.stop(run, "cannot be checked while GitHub fails")
        # Written after a failed poll too, so it still shows the supervisor is alive.
        try:
            vms.write_status(
                STATUS_FILE, status(state, time.time(), github_ok, vms.mem_available_mib())
            )
            status_error = ""
        except Exception as e:
            why = f"{type(e).__name__}: {e}"
            if why != status_error:
                log(f"could not write the status file: {why}")
            status_error = why
        if args.once:
            while state.launches:
                time.sleep(POLL_SECONDS)
                vms.reap(gh, state, time.time())
            return 0
        job_waits.rest(state, wait)


if __name__ == "__main__":
    telemetry.start()
    sys.exit(main())
