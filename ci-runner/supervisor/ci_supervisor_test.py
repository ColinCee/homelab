import ast
import json
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Never, Protocol

import cache_disks
import ci_supervisor as sup
import github_api
import install_host
import job_waits
import load_testing
import pytest
import vms

# The other modules' fixtures, the autouse ones too, as a poll runs all of their code.
from cache_disks_test import (
    fixture_caches,  # noqa: F401
    fixture_outbox_dir,  # noqa: F401
)
from github_api_test import OneRepoBroken
from job_waits_test import (
    fixture_wait_metrics,  # noqa: F401
    fixture_waits_file,  # noqa: F401
)
from vms_test import (
    READ_DISK_WAIT,
    fixture_plenty_of_memory,  # noqa: F401
    fixture_vm_records,  # noqa: F401
)

from conftest import NoGitHub, linux_only


@pytest.fixture(autouse=True)
def no_load_test(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """The machine running the tests has no drain on and no load test record."""
    folder = tmp_path_factory.mktemp("load-test")
    monkeypatch.setattr(load_testing, "DRAIN_FILE", folder / "drain")
    monkeypatch.setattr(load_testing, "LOAD_TEST_FILE", folder / "load-test.json")


def test_the_standby_goes_to_the_repo_of_the_last_job() -> None:
    repos = ["a/x", "a/y", "a/z"]
    assert sup.standby_first(repos, "a/y") == ["a/y", "a/x", "a/z"]
    assert sup.standby_first(repos, "") == repos
    assert sup.standby_first(repos, "a/gone") == repos


X: github_api.Want = ("a/x", "")
Y: github_api.Want = ("b/y", "")


@pytest.mark.parametrize(
    ("queued", "booting", "free", "starts"),
    [
        ([X] * 3, {}, 2, [X, X]),
        ([X] * 2, {X: 1}, 2, [X]),  # one VM is already booting for it
        ([Y, X], {}, 1, [Y]),  # the oldest job first, whatever its name
        ([X, Y, X], {X: 1}, 1, [Y]),  # a booting VM covers the oldest
        ([X], {}, 0, []),
    ],
)
def test_plans_one_vm_per_uncovered_job_oldest_first(
    queued: list[github_api.Want],
    booting: dict[github_api.Want, int],
    free: int,
    starts: list[github_api.Want],
) -> None:
    assert sup.plan(queued, booting, free) == starts


def test_keeps_jobs_with_different_cache_labels_apart() -> None:
    plain = github_api.Job("octo/agent", 1, "check", ("homelab-fc",))
    labelled = github_api.Job("octo/agent", 2, "build", ("homelab-fc", "FC-CACHE-octo--widgets"))
    assert plain.want == ("octo/agent", "")
    assert labelled.want == ("octo/agent", "fc-cache-octo--widgets")

    # A standby (or booting VM) without the label does not cover the labelled job.
    starts = sup.plan([plain.want, labelled.want], {plain.want: 1}, 2)

    assert starts == [labelled.want]


@pytest.mark.parametrize("fails", ["check_served", "find_queued"])
def test_a_repo_github_will_not_show_never_stops_the_others(
    monkeypatch: pytest.MonkeyPatch, fails: str
) -> None:
    def broken[T](repo: str, value: T) -> T:
        if repo == "o/gone":
            raise OSError("HTTP Error 404: Not Found")
        return value

    monkeypatch.setattr(
        github_api,
        "check_served",
        lambda gh, state, repo, now, fresh=False: (
            broken(repo, True) if fails == "check_served" else True
        ),
    )
    job = github_api.Job("o/ok", 1, "check", tuple(github_api.RUNNER_LABELS))
    monkeypatch.setattr(
        github_api,
        "find_queued",
        lambda gh, repo, **_: (
            broken(repo, [job] if repo == "o/ok" else [])
            if fails == "find_queued"
            else ([job] if repo == "o/ok" else [])
        ),
    )
    monkeypatch.setattr(vms, "reap", lambda *a, **k: None)
    monkeypatch.setattr(cache_disks, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    launched: list[str] = []
    monkeypatch.setattr(
        vms, "launch", lambda gh, state, repo, slot, now, **k: launched.append(repo)
    )
    sup.tick(OneRepoBroken("o/gone"), sup.State(), ["o/gone", "o/ok"], 0.0)
    assert launched == ["o/ok"]


def test_a_rate_limit_still_ends_the_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    def limited(gh: github_api.GitHubApi, state: sup.State, repo: str, now: float) -> Never:
        raise github_api.RateLimited(0.0)

    monkeypatch.setattr(github_api, "check_served", limited)
    with pytest.raises(github_api.RateLimited):
        sup.tick(OneRepoBroken("o/gone"), sup.State(), ["o/ok"], 0.0)


def test_quiet_repos_are_polled_once_a_minute_used_ones_every_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cache_disks, "CACHE_DIR", tmp_path)
    state = sup.State(used={"a/busy"}, polled={"a/quiet": 100.0, "a/busy": 100.0})
    assert sup.due(["a/busy", "a/quiet", "a/new"], state, 130.0) == ["a/busy", "a/new"]
    assert sup.due(["a/busy", "a/quiet"], state, 161.0) == ["a/busy", "a/quiet"]


def test_a_poll_asks_only_a_few_quiet_repos_the_longest_unasked_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cache_disks, "CACHE_DIR", tmp_path)
    quiet = [f"a/q{i}" for i in range(10)]
    state = sup.State(used={"a/busy"}, polled={r: 100.0 - i for i, r in enumerate(quiet)})
    polling = sup.due(["a/busy", *quiet], state, 200.0)
    assert polling == ["a/busy", "a/q7", "a/q8", "a/q9"]
    assert len(polling) == 1 + sup.QUIET_PER_POLL


def test_after_a_restart_the_standby_goes_where_the_last_job_ran(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(vms, "LAST_REPO_FILE", tmp_path / "last-repo")
    assert vms.recalled_last_job() == ("", "")
    vms.remember_last_job("a/y", "fc-cache-a--z")
    assert vms.recalled_last_job() == ("a/y", "fc-cache-a--z")
    vms.remember_last_job("a/y", "")
    last, _ = vms.recalled_last_job()
    assert last == "a/y"
    repos = ["a/x", "a/y", "a/z"]
    state = sup.State(used={last})
    # Polled every time, first, so the standby boots for it and not for the first listed.
    assert sup.due(sup.standby_first(repos, last), state, 0.0)[0] == "a/y"


def test_a_slow_poll_says_what_it_spent_its_time_on() -> None:
    line = sup.slow_poll(9.0, 40, 8.5, {"a/x": 1.0, "a/y": 6.0, "a/z": 0.5, "a/w": 0.2})
    assert line == (
        "slow poll: 9.0s; 40 GitHub requests took 8.5s in all; asking 4 repos for queued jobs "
        "at once took 6.0s (slowest a/y 6.0s, a/x 1.0s, a/z 0.5s); the rest 3.0s"
    )


def _launch(slot: int, standby: bool) -> vms.Launch:
    # Only the counts are read: no process or registration file is needed.
    return vms.Launch(slot, "octo/agent", slot, f"fc-{slot}", 0.0, Path("m"), standby)


def test_the_status_file_counts_busy_and_standby_vms_and_cache_disks(caches: Path) -> None:
    (caches / "octo__agent.ext4").touch()
    (caches / "octo__agent.json").touch()  # which artifact built it, not a disk
    state = sup.State(
        slots=5, launches={0: _launch(0, False), 1: _launch(1, False), 2: _launch(2, True)}
    )

    state.held = {7: "octo/agent"}

    assert sup.status(state, 1700000000.5, github_ok=True, mem_available=9000) == {
        "updated": 1700000000,
        "slots": 5,
        "in_use": 2,
        "standby": 1,
        "cache_disks": ["octo/agent", "octo/widgets"],
        "github_ok": True,
        "mem_available_mib": 9000,
        "held_for_memory": 1,
        "queued": 0,
        "in_use_repos": {"octo/agent": 2},
        "draining": False,
        "load_test": None,
    }


def test_vms_start_only_in_the_hosts_slots(caches: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_api, "check_served", lambda *a, **k: True)
    monkeypatch.setattr(github_api, "only_colin", lambda *a, **k: True)
    monkeypatch.setattr(
        github_api,
        "find_queued",
        lambda gh, repo, **_: [
            github_api.Job(repo, n, "j", tuple(github_api.RUNNER_LABELS)) for n in range(9)
        ],
    )
    monkeypatch.setattr(vms, "reap", lambda *a, **k: None)
    monkeypatch.setattr(cache_disks, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    slots: list[int] = []
    monkeypatch.setattr(vms, "launch", lambda gh, state, repo, slot, now, **k: slots.append(slot))
    sup.tick(NoGitHub(), sup.State(slots=5), ["o/ok"], 0.0)
    assert slots == [0, 1, 2, 3, 4]


class QueueJobs(Protocol):
    def __call__(
        self, jobs: int, free_mib: int, queued_at: float = 0.0, workflow: str = ""
    ) -> None: ...


# Queues jobs, and the (repo, MiB, standby) of each VM started and the log lines.
type Host = tuple[QueueJobs, list[tuple[str, int, bool]], list[str]]


@pytest.fixture
def host(caches: Path, monkeypatch: pytest.MonkeyPatch, supervisor_log: list[str]) -> Host:
    """One served repo with jobs queued, and a launch that records the VMs it starts."""
    monkeypatch.setattr(github_api, "check_served", lambda *a, **k: True)
    monkeypatch.setattr(github_api, "only_colin", lambda *a, **k: True)
    monkeypatch.setattr(vms, "reap", lambda *a, **k: None)
    monkeypatch.setattr(cache_disks, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(vms, "has_job", lambda gh, run: False)  # VMs are still starting
    started: list[tuple[str, int, bool]] = []

    def launch(
        gh: github_api.GitHubApi,
        state: sup.State,
        repo: str,
        slot: int,
        now: float,
        *,
        cache: Path | None,
        label: str = "",
        standby: bool = False,
        jobs: Iterable[int] = (),
        load_test: object = None,
    ) -> None:
        mem = vms.next_size(state, standby)[1]
        started.append((repo, mem, standby))
        run = _launch(slot, standby)
        run.repo, run.label = repo, label
        state.launches[slot] = run

    monkeypatch.setattr(vms, "launch", launch)

    def queue(jobs: int, free_mib: int, queued_at: float = 0.0, workflow: str = "") -> None:
        monkeypatch.setattr(
            github_api,
            "find_queued",
            lambda gh, repo, **_: (
                [
                    github_api.Job(
                        repo,
                        n,
                        "j",
                        tuple(github_api.RUNNER_LABELS),
                        queued_at=queued_at,
                        workflow=workflow,
                    )
                    for n in range(jobs)
                ]
                if repo == "o/ok"
                else []
            ),
        )
        monkeypatch.setattr(vms, "mem_available_mib", lambda: free_mib)

    return queue, started, supervisor_log


def test_vms_start_while_memory_leaves_the_reserve(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    queue(jobs=3, free_mib=3072 + 4096 + 2048 + 2048)
    state = sup.State()
    sup.tick(NoGitHub(), state, ["o/ok"], 0.0)
    assert started == [("o/ok", 4096, False), ("o/ok", 2048, False), ("o/ok", 2048, False)]
    assert state.held == {}


def test_a_job_waits_queued_when_memory_is_short_and_is_logged_once(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, started, lines = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    queue(jobs=2, free_mib=3072 + 4096 + 2047)  # room for the first VM only
    state = sup.State()
    sup.tick(NoGitHub(), state, ["o/ok"], 0.0)
    assert started == [("o/ok", 4096, False)]
    assert state.held == {1: "o/ok"}

    queue(jobs=2, free_mib=3072 + 2047)  # still short, the first job's VM is still booting
    sup.tick(NoGitHub(), state, ["o/ok"], 2.0)
    assert started == [("o/ok", 4096, False)]
    assert [line for line in lines if "memory" in line] == [
        "job 1 in o/ok has waited 0m00s here: memory"
        " (2047 MiB free above the 3072 MiB reserve, a VM needs 2048)"
    ]
    status = sup.status(state, 2.0, github_ok=True, mem_available=5119)
    assert status["held_for_memory"] == 1 and status["mem_available_mib"] == 5119

    queue(jobs=2, free_mib=3072 + 2048)  # memory freed: the job starts
    sup.tick(NoGitHub(), state, ["o/ok"], 4.0)
    assert started[-1] == ("o/ok", 2048, False)
    assert state.held == {}


def test_no_vm_starts_while_the_disk_is_busy(
    host: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The Beelink's /proc/pressure/io at 21:28 on 5 Nov, with 9 VMs and Grafana failing.
    pressure = tmp_path / "io"
    pressure.write_text(
        "some avg10=92.99 avg60=90.49 avg300=86.69 total=53934832649\n"
        "full avg10=27.79 avg60=26.06 avg300=27.47 total=33010242903\n"
    )
    monkeypatch.setattr(vms, "IO_PRESSURE", pressure)
    monkeypatch.setattr(vms, "disk_wait_percent", READ_DISK_WAIT)
    queue, started, lines = host
    monkeypatch.setattr(sup, "standby_count", lambda: 1)
    queue(jobs=1, free_mib=1 << 20)
    state = sup.State()
    sup.tick(NoGitHub(), state, ["o/ok"], 0.0)
    assert started == [] and state.held == {0: "o/ok"}
    assert lines[-1] == (
        "job 0 in o/ok has waited 0m00s here: the disk (tasks waited on it 90% of the last minute)"
    )

    pressure.write_text("some avg10=12.00 avg60=13.63 avg300=13.00 total=1\n")
    sup.tick(NoGitHub(), state, ["o/ok"], 2.0)
    assert started[0][0] == "o/ok" and state.held == {}


def test_a_half_busy_disk_starts_one_vm_a_minute(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, started, lines = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    monkeypatch.setattr(vms, "disk_wait_percent", lambda: 40.0)
    queue(jobs=3, free_mib=1 << 20)
    state = sup.State()
    sup.tick(NoGitHub(), state, ["o/ok"], 0.0)
    assert len(started) == 1
    assert (
        "job 1 in o/ok has waited 0m00s here: the disk (tasks waited on it 40% of the last minute)"
    ) in lines
    sup.tick(NoGitHub(), state, ["o/ok"], 30.0)
    assert len(started) == 1  # the first VM's load has not shown yet
    sup.tick(NoGitHub(), state, ["o/ok"], 61.0)
    assert len(started) == 2


def test_a_job_with_no_free_slot_is_logged_with_its_wait_every_few_minutes(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, started, lines = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    monkeypatch.setattr(vms, "on_its_way", lambda *a: False)  # the first VM has its job
    queue(jobs=2, free_mib=1 << 20)
    state = sup.State(slots=1)
    sup.tick(NoGitHub(), state, ["o/ok"], 0.0)
    second = [
        github_api.Job("o/ok", 1, "j", tuple(github_api.RUNNER_LABELS))
    ]  # the first has its VM
    monkeypatch.setattr(
        github_api, "find_queued", lambda gh, repo, **_: second if repo == "o/ok" else []
    )
    for now in (2.0, 179.0, 200.0):
        sup.tick(NoGitHub(), state, ["o/ok"], now)
    assert len(started) == 1
    assert lines == [
        "job 1 in o/ok has waited 0m00s here: all 1 build slots in use",
        "job 1 in o/ok has waited 3m20s here: all 1 build slots in use",
    ]
    queue(jobs=0, free_mib=1 << 20)
    sup.tick(NoGitHub(), state, ["o/ok"], 230.0)
    assert lines[-1] == "job 1 in o/ok stopped waiting after 3m50s"


def test_a_job_whose_vm_is_booting_is_logged_only_after_a_long_wait(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, started, lines = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    monkeypatch.setattr(vms, "on_its_way", lambda *a: True)  # GitHub never gives it the job
    queue(jobs=1, free_mib=1 << 20)
    state = sup.State()
    for now in (0.0, 2.0, 100.0):
        sup.tick(NoGitHub(), state, ["o/ok"], now)
    assert len(started) == 1 and lines == []
    sup.tick(NoGitHub(), state, ["o/ok"], 182.0)
    assert lines == [f"job 0 in o/ok has waited 3m02s here: {job_waits.BOOTING}"]


def test_a_job_whose_vm_is_ready_is_counted_apart_from_its_boot(
    host: Host, waits_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, _, lines = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)

    def online(
        gh: github_api.GitHubApi, run: vms.Launch
    ) -> bool:  # the runner is up and idle from the second poll
        run.ready = True
        return False

    monkeypatch.setattr(vms, "has_job", online)
    state = sup.State()
    queue(jobs=1, free_mib=1 << 20)
    sup.tick(NoGitHub(), state, ["o/ok"], 0.0)  # its VM boots
    sup.tick(NoGitHub(), state, ["o/ok"], 10.0)
    assert state.waits[0].why == job_waits.READY
    queue(jobs=0, free_mib=1 << 20)  # GitHub gave it the job
    sup.tick(NoGitHub(), state, ["o/ok"], 40.0)
    record = json.loads(waits_file.read_text())
    assert (record["booting"], record["ready"]) == (10, 30)
    assert lines == []  # a short wait is not logged


def test_a_job_github_queued_long_before_it_was_seen_is_logged_at_once(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, _, lines = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    state = sup.State()
    queue(jobs=1, free_mib=1 << 20, queued_at=1000.0)
    sup.tick(NoGitHub(), state, ["o/ok"], 1600.0)
    queue(jobs=0, free_mib=1 << 20)
    sup.tick(NoGitHub(), state, ["o/ok"], 1610.0)
    assert lines == [
        "job 0 in o/ok has waited 0m00s here (GitHub had queued it 10m00s before): "
        + job_waits.BOOTING,
        "job 0 in o/ok stopped waiting after 0m10s, 10m10s since GitHub queued it",
    ]


def test_a_held_job_is_recorded_with_its_seconds_per_kind_when_it_starts(
    host: Host, waits_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    monkeypatch.setattr(vms, "on_its_way", lambda *a: True)
    state = sup.State()
    queue(jobs=1, free_mib=3072 + 2047)  # held for memory
    sup.tick(NoGitHub(), state, ["o/ok"], 100.0)
    sup.tick(NoGitHub(), state, ["o/ok"], 130.0)
    monkeypatch.setattr(vms, "disk_wait_percent", lambda: 90.0)
    sup.tick(NoGitHub(), state, ["o/ok"], 160.0)  # held for the disk
    monkeypatch.setattr(vms, "disk_wait_percent", lambda: 0.0)
    queue(jobs=1, free_mib=1 << 20)
    sup.tick(NoGitHub(), state, ["o/ok"], 170.0)  # its VM boots
    assert len(started) == 1 and not waits_file.exists()
    sup.tick(NoGitHub(), state, ["o/ok"], 185.0)
    queue(jobs=0, free_mib=1 << 20)  # the job started
    sup.tick(NoGitHub(), state, ["o/ok"], 200.0)
    assert [json.loads(line) for line in waits_file.read_text().splitlines()] == [
        {"job": 0, "repo": "o/ok", "since": 100, "until": 200,
         "memory": 60, "disk": 10, "slots": 0, "booting": 30, "ready": 0, "unseen": 0,
         "other": 0, "why": "memory (2047 MiB free above the 3072 MiB reserve, a VM needs 4096)"}
    ]  # fmt: skip
    linux_only("POSIX file modes")
    assert waits_file.stat().st_mode & 0o777 == 0o644


def test_a_job_a_standby_takes_is_recorded_with_its_seconds_and_reason(
    host: Host, waits_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    monkeypatch.setattr(vms, "on_its_way", lambda *a: False)  # the busy VM has its own job
    queue(jobs=1, free_mib=1 << 20)
    state = sup.State(slots=1, launches={0: _launch(0, False)})
    state.launches[0].repo = "o/ok"
    sup.tick(NoGitHub(), state, ["o/ok"], 0.0)
    sup.tick(NoGitHub(), state, ["o/ok"], 100.0)
    # That VM ends and a standby, booted before the job queued, is up in its slot.
    standby = state.launches[0] = _launch(0, True)
    standby.repo, standby.ready = "o/ok", True
    monkeypatch.setattr(vms, "on_its_way", lambda *a: True)
    sup.tick(NoGitHub(), state, ["o/ok"], 102.0)
    queue(jobs=0, free_mib=1 << 20)  # GitHub gave the standby the job
    sup.tick(NoGitHub(), state, ["o/ok"], 104.0)
    record = json.loads(waits_file.read_text())
    assert started == [] and (record["job"], record["slots"], record["ready"]) == (0, 102, 2)
    assert record["why"] == "all 1 build slots in use"


def test_a_job_taken_by_a_vm_booted_for_another_is_recorded_with_its_seconds_and_reason(
    host: Host, waits_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    monkeypatch.setattr(vms, "on_its_way", lambda *a: True)
    queue(jobs=2, free_mib=1 << 20)
    state = sup.State(slots=1)
    sup.tick(NoGitHub(), state, ["o/ok"], 0.0)  # a VM boots for job 0
    sup.tick(NoGitHub(), state, ["o/ok"], 50.0)
    assert len(started) == 1 and state.waits[1].why == "all 1 build slots in use"
    # GitHub gives that VM job 1, not job 0 it booted for.
    first = [github_api.Job("o/ok", 0, "j", tuple(github_api.RUNNER_LABELS))]
    monkeypatch.setattr(
        github_api, "find_queued", lambda gh, repo, **_: first if repo == "o/ok" else []
    )
    sup.tick(NoGitHub(), state, ["o/ok"], 60.0)
    record = json.loads(waits_file.read_text())
    assert (record["job"], record["slots"], record["why"]) == (1, 60, "all 1 build slots in use")


def test_the_longest_waits_seconds_per_kind_go_to_prometheus(
    host: Host, wait_metrics: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, _, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    monkeypatch.setattr(vms, "on_its_way", lambda *a: True)
    state = sup.State()
    # Held for memory, GitHub's for 60 s.
    queue(jobs=1, free_mib=3072 + 2047, queued_at=40.0, workflow="watch")
    sup.tick(NoGitHub(), state, ["o/ok"], 100.0)
    assert 'homelab_ci_wait_seconds{kind="memory"} 0' in wait_metrics.read_text()
    sup.tick(NoGitHub(), state, ["o/ok"], 110.0)  # under 15 s: not written
    queue(jobs=1, free_mib=1 << 20, queued_at=40.0)
    sup.tick(NoGitHub(), state, ["o/ok"], 130.0)  # its VM boots
    sup.tick(NoGitHub(), state, ["o/ok"], 150.0)
    slots = state.slots
    assert wait_metrics.read_text().splitlines() == [
        'homelab_ci_wait_seconds{kind="memory"} 30',
        'homelab_ci_wait_seconds{kind="disk"} 0',
        'homelab_ci_wait_seconds{kind="slots"} 0',
        'homelab_ci_wait_seconds{kind="booting"} 20',
        'homelab_ci_wait_seconds{kind="ready"} 0',
        'homelab_ci_wait_seconds{kind="unseen"} 60',
        'homelab_ci_wait_seconds{kind="other"} 0',
        'homelab_ci_queued_seconds{repo="o/ok",workflow="watch"} 110',
        "homelab_ci_vms_busy 1",
        f"homelab_ci_vm_slots {slots}",
        "homelab_ci_poll_failures 0",
        "homelab_ci_poll_failed_seconds 0",
    ]
    linux_only("POSIX file modes")
    assert wait_metrics.stat().st_mode & 0o777 == 0o644
    queue(jobs=0, free_mib=1 << 20)  # the job started: nothing waits
    sup.tick(NoGitHub(), state, ["o/ok"], 170.0)
    lines = wait_metrics.read_text().splitlines()
    assert {line.split()[1] for line in lines if "wait_seconds" in line} == {"0"}
    assert not [line for line in lines if "queued_seconds" in line]


def test_a_job_asking_for_a_label_no_vm_has_is_logged_named_and_never_started(
    host: Host, wait_metrics: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, started, lines = host
    runs = [{"id": 1}]
    jobs = {1: [{"id": 7, "name": "gpu", "status": "queued", "labels": ["homelab-fc", "gpu"]}]}
    found = github_api.queued_jobs("o/ok", runs, jobs.__getitem__)
    assert [(job.id, job.ours) for job in found] == [(7, False)]
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    monkeypatch.setattr(vms, "mem_available_mib", lambda: 1 << 20)
    monkeypatch.setattr(
        github_api, "find_queued", lambda gh, repo, **_: found if repo == "o/ok" else []
    )
    state = sup.State()
    sup.tick(NoGitHub(), state, ["o/ok"], 0.0)
    sup.tick(NoGitHub(), state, ["o/ok"], 20.0)
    assert started == []
    assert lines == ["job 7 in o/ok has waited 0m00s here: no VM has its labels (homelab-fc, gpu)"]
    # The alert names the repo and labels, so nobody has to search the journal for them.
    assert 'homelab_ci_wait_seconds{kind="other",repo="o/ok",labels="homelab-fc,gpu"} 20' in (
        wait_metrics.read_text().splitlines()
    )


def test_a_held_job_in_a_repo_not_polled_this_time_stays_held(host: Host) -> None:
    queue, _, _ = host
    queue(jobs=0, free_mib=1 << 20)
    state = sup.State(held={5: "o/quiet"})
    sup.tick(NoGitHub(), state, ["o/ok"], 0.0)
    assert state.held == {5: "o/quiet"}


def test_a_standby_gives_way_to_a_job_waiting_for_memory(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 1)
    stopped: list[str] = []
    monkeypatch.setattr(
        vms, "stop_if_still_idle", lambda gh, run, why: stopped.append(f"{run.repo} {why}")
    )
    standby = _launch(0, True)  # waiting ready for octo/agent, holding 4 GiB
    state = sup.State(launches={0: standby})
    queue(jobs=1, free_mib=3072 + 1024)  # a free slot, but not the memory for the job
    sup.tick(NoGitHub(), state, ["o/ok", "octo/agent"], 0.0)
    assert started == []  # neither the job nor another standby
    assert state.held == {0: "o/ok"}
    assert stopped == ["octo/agent is in the way of a queued job"]


def test_no_standby_boots_while_a_job_waits_for_memory(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 1)
    queue(jobs=0, free_mib=1 << 20)
    state = sup.State(held={5: "o/quiet"})
    sup.tick(NoGitHub(), state, ["o/ok"], 0.0)
    assert started == []
    state.held = {}
    sup.tick(NoGitHub(), state, ["o/ok"], 2.0)
    assert started == [("o/ok", 4096, True)]


def test_a_standby_boots_only_with_memory_for_it(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 1)
    queue(jobs=0, free_mib=3072 + 4095)
    sup.tick(NoGitHub(), sup.State(), ["o/ok"], 0.0)
    assert started == []


def test_the_polled_repos_are_asked_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    repos = ["o/a", "o/b", "o/c", "o/d"]
    # Each answer waits until every repo is being asked: one after another, it would time out.
    together = threading.Barrier(len(repos), timeout=5)

    def find(gh: github_api.GitHubApi, repo: str, **_: object) -> list[github_api.Job]:
        together.wait()
        return [github_api.Job(repo, 1, "check", tuple(github_api.RUNNER_LABELS))]

    monkeypatch.setattr(github_api, "check_served", lambda *a, **k: True)
    monkeypatch.setattr(github_api, "only_colin", lambda *a, **k: True)
    monkeypatch.setattr(github_api, "find_queued", find)
    monkeypatch.setattr(vms, "reap", lambda *a, **k: None)
    monkeypatch.setattr(cache_disks, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    launched: list[str] = []
    monkeypatch.setattr(
        vms, "launch", lambda gh, state, repo, slot, now, **k: launched.append(repo)
    )
    spent = sup.tick(NoGitHub(), sup.State(slots=5), repos, 0.0)
    assert launched == repos  # in the order given, whatever order the answers came in
    assert list(spent) == repos


class QueueBuilds(Protocol):
    def __call__(self, build: int) -> None: ...


# Queues build jobs, and the (slot, label) of each VM started.
type Pools = tuple[QueueBuilds, list[tuple[int, str]]]


@pytest.fixture
def pools(caches: Path, monkeypatch: pytest.MonkeyPatch) -> Pools:
    """Build jobs queued in one repo; launch records (slot, label)."""
    monkeypatch.setattr(github_api, "check_served", lambda *a, **k: True)
    monkeypatch.setattr(github_api, "only_colin", lambda *a, **k: True)
    monkeypatch.setattr(vms, "reap", lambda *a, **k: None)
    monkeypatch.setattr(cache_disks, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    started: list[tuple[int, str]] = []

    def launch(
        gh: github_api.GitHubApi,
        state: sup.State,
        repo: str,
        slot: int,
        now: float,
        *,
        cache: Path | None,
        label: str = "",
        standby: bool = False,
        jobs: Iterable[int] = (),
        load_test: object = None,
    ) -> None:
        started.append((slot, label))
        state.launches[slot] = vms.Launch(
            slot, repo, slot, f"fc-{slot}", now, Path("m"), standby, label=label
        )

    monkeypatch.setattr(vms, "launch", launch)

    def queue(build: int) -> None:
        jobs = [
            github_api.Job("o/ok", n, "b", tuple(github_api.RUNNER_LABELS)) for n in range(build)
        ]
        monkeypatch.setattr(github_api, "find_queued", lambda gh, repo, **_: jobs)

    return queue, started


def test_the_standby_after_a_labelled_job_registers_with_its_label_and_disk(
    caches: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered: list[list[str]] = []
    disks: list[str] = []

    class Registers(NoGitHub):
        def request(
            self, method: str, path: str, body: Mapping[str, Any] | None = None
        ) -> dict[str, Any]:
            if method == "GET":  # the Builder's VM has its job
                return {"busy": True}
            assert body is not None
            registered.append(body["labels"])
            return {"runner": {"id": len(registered)}, "encoded_jit_config": "x"}

    def start_vm(run: vms.Launch, timeout: int, env: Mapping[str, str]) -> None:
        disks.append(env["CACHE_IMAGE"])

    for name in ("check_served", "only_colin"):
        monkeypatch.setattr(github_api, name, lambda *a, **k: True)
    monkeypatch.setattr(vms, "reap", lambda *a, **k: None)
    monkeypatch.setattr(cache_disks, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(sup, "standby_count", lambda: 1)
    monkeypatch.setattr(vms, "STATE_DIR", caches)
    monkeypatch.setattr(vms, "LAST_REPO_FILE", caches / "last-repo")
    monkeypatch.setattr(vms, "start_vm", start_vm)
    stopped: list[str] = []
    monkeypatch.setattr(vms, "stop_if_still_idle", lambda gh, run, why: stopped.append(why))
    labelled = (*github_api.RUNNER_LABELS, "fc-cache-octo--widgets")
    jobs: list[github_api.Job] = []
    monkeypatch.setattr(
        github_api, "find_queued", lambda gh, repo, **_: jobs if repo == "octo/agent" else []
    )
    repos = ["octo/agent", "octo/widgets"]
    state = sup.State(slots=2)

    # A task's Builder asks for another repo's cache disk: its VM and the next standby both
    # register with that label and get that disk.
    jobs[:] = [github_api.Job("octo/agent", 1, "build", labelled)]
    sup.tick(Registers(), state, repos, 0.0)
    assert registered == [[*labelled], [*labelled]]
    assert disks == [str(caches / "octo__widgets.ext4")] * 2
    assert [run.standby for run in state.launches.values()] == [False, True]
    assert vms.recalled_last_job() == ("octo/agent", "fc-cache-octo--widgets")

    # Its Reviewer, asking for the same, is left to the standby: no VM boots, none is stopped.
    jobs[:] = [github_api.Job("octo/agent", 2, "review", labelled)]
    sup.tick(Registers(), state, repos, 100.0)
    assert len(registered) == 2 and stopped == []

    # A job wanting something else, with no free slot, still gets the standby out of its way.
    jobs[:] = [github_api.Job("octo/agent", 3, "check", tuple(github_api.RUNNER_LABELS))]
    sup.tick(Registers(), state, repos, 200.0)
    assert stopped == ["is in the way of a queued job"]


def test_a_builders_reviewer_takes_a_standby_booted_when_its_builders_vm_ended(
    caches: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered: list[list[str]] = []

    class Registers(NoGitHub):
        def request(
            self, method: str, path: str, body: Mapping[str, Any] | None = None
        ) -> dict[str, Any]:
            if method == "GET":  # every runner has its job, or is still connecting
                return {"busy": True}
            assert body is not None
            registered.append(body["labels"])
            return {"runner": {"id": len(registered)}, "encoded_jit_config": "x"}

    (caches / "octo__gears.ext4").touch()
    for name in ("check_served", "only_colin"):
        monkeypatch.setattr(github_api, name, lambda *a, **k: True)
    monkeypatch.setattr(cache_disks, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(github_api, "delete_runner", lambda *a, **k: True)
    monkeypatch.setattr(sup, "standby_count", lambda: 1)
    monkeypatch.setattr(vms, "STATE_DIR", caches)
    monkeypatch.setattr(vms, "LAST_REPO_FILE", caches / "last-repo")
    stopped: list[str] = []
    monkeypatch.setattr(vms, "stop_if_still_idle", lambda gh, run, why: stopped.append(why))
    widgets = (*github_api.RUNNER_LABELS, "fc-cache-octo--widgets")
    jobs: list[github_api.Job] = []
    monkeypatch.setattr(
        github_api, "find_queued", lambda gh, repo, **_: jobs if repo == "octo/agent" else []
    )
    repos = ["octo/agent", "octo/widgets", "octo/gears"]
    # Task A's Builder runs on widgets' disk; task B's, started after it, on gears'.
    builder = vms.Launch(0, "octo/agent", 1, "fc-0-1", 0.0, caches / "m0", label=widgets[-1])
    other = vms.Launch(
        1, "octo/agent", 2, "fc-1-1", 10.0, caches / "m1", label="fc-cache-octo--gears"
    )
    state = sup.State(slots=2, launches={0: builder, 1: other})
    state.last_repo, state.last_label = "octo/agent", other.label
    # A's Builder ends before GitHub queues its Reviewer: the standby boots in its slot for
    # widgets, not for gears, the last job started.
    monkeypatch.setattr(
        vms, "running_vms", lambda: {r.unit for r in state.launches.values()} - {builder.unit}
    )
    sup.tick(Registers(), state, repos, 600.0)
    assert registered == [[*widgets]] and state.launches[0].standby
    assert vms.recalled_last_job() == ("octo/agent", "fc-cache-octo--widgets")

    # The Reviewer, queued on the next poll, is left to that standby: no VM boots for it and
    # none is stopped.
    jobs[:] = [github_api.Job("octo/agent", 2, "review", widgets)]
    sup.tick(Registers(), state, repos, 602.0)
    assert len(registered) == 1 and "is in the way of a queued job" not in stopped


def test_a_reviewer_takes_a_standby_booted_while_its_builder_ran(
    caches: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered: list[list[str]] = []

    class Registers(NoGitHub):
        def request(
            self, method: str, path: str, body: Mapping[str, Any] | None = None
        ) -> dict[str, Any]:
            if method == "GET":
                return {"busy": True}
            assert body is not None
            registered.append(body["labels"])
            return {"runner": {"id": len(registered)}, "encoded_jit_config": "x"}

    for name in ("check_served", "only_colin"):
        monkeypatch.setattr(github_api, name, lambda *a, **k: True)
    monkeypatch.setattr(cache_disks, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(github_api, "delete_runner", lambda *a, **k: True)
    monkeypatch.setattr(sup, "standby_count", lambda: 2)
    monkeypatch.setattr(vms, "STATE_DIR", caches)
    monkeypatch.setattr(vms, "LAST_REPO_FILE", caches / "last-repo")
    stopped: list[str] = []
    monkeypatch.setattr(vms, "stop_if_still_idle", lambda gh, run, why: stopped.append(why))
    widgets = (*github_api.RUNNER_LABELS, "fc-cache-octo--widgets")
    jobs: list[github_api.Job] = []
    monkeypatch.setattr(
        github_api, "find_queued", lambda gh, repo, **_: jobs if repo == "octo/agent" else []
    )
    repos = ["octo/agent"]
    # Task A's Builder runs on widgets' disk; task B's build, started after it, on gears'.
    builder = vms.Launch(0, "octo/agent", 1, "fc-0-1", 0.0, caches / "m0", label=widgets[-1])
    other = vms.Launch(
        1, "octo/agent", 2, "fc-1-1", 10.0, caches / "m1", label="fc-cache-octo--gears"
    )
    state = sup.State(slots=3, launches={0: builder, 1: other})
    state.last_repo, state.last_label = "octo/agent", other.label
    monkeypatch.setattr(vms, "running_vms", lambda: {r.unit for r in state.launches.values()})
    # While both builds run, the free slot's standby boots for A's, the older, not B's, the last
    # started.
    sup.tick(Registers(), state, repos, 20.0)
    assert registered == [[*widgets]] and state.launches[2].standby

    # A's Builder ends and its Reviewer queues: the booted standby takes it, nothing boots
    # for it and nothing is stopped; the freed slot's standby goes to B's label.
    monkeypatch.setattr(
        vms, "running_vms", lambda: {r.unit for r in state.launches.values()} - {builder.unit}
    )
    jobs[:] = [github_api.Job("octo/agent", 2, "review", widgets)]
    sup.tick(Registers(), state, repos, 22.0)
    assert registered == [[*widgets], [*github_api.RUNNER_LABELS, "fc-cache-octo--gears"]]
    assert all(run.standby for run in state.launches.values() if run.slot != 1)
    assert stopped == []


def test_a_second_standby_boots_only_with_a_spare_slot_and_memory(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 2)
    standby = _launch(0, True)
    standby.repo = "o/ok"
    running = _launch(1, False)
    running.repo, running.label = "o/ok", "fc-cache-o--x"
    queue(jobs=0, free_mib=1 << 20)
    state = sup.State(slots=2, launches={0: standby, 1: running})
    sup.tick(NoGitHub(), state, ["o/ok"], 0.0)
    assert started == []  # no free slot
    state.slots = 3
    queue(jobs=0, free_mib=3072 + 4095)
    sup.tick(NoGitHub(), state, ["o/ok"], 2.0)
    assert started == []  # no memory above the reserve
    queue(jobs=0, free_mib=3072 + 4096)
    sup.tick(NoGitHub(), state, ["o/ok"], 4.0)
    assert started == [("o/ok", 4096, True)]
    sup.tick(NoGitHub(), state, ["o/ok"], 6.0)
    assert len(started) == 1  # two wait already


@pytest.mark.parametrize(("count", "boots"), [(2, 2), (1, 1)])
def test_a_second_standby_boots_for_the_same_repo_and_label(
    host: Host, monkeypatch: pytest.MonkeyPatch, count: int, boots: int
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: count)
    queue(jobs=0, free_mib=1 << 20)
    state = sup.State(slots=2)
    for now in (0.0, 2.0, 4.0):
        sup.tick(NoGitHub(), state, ["o/ok"], now)
    assert started == [("o/ok", 4096, True)] * boots


def test_each_wanted_repo_gets_a_standby_before_any_gets_a_second(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 2)
    running = _launch(0, False)
    running.repo = "o/b"
    queue(jobs=0, free_mib=1 << 20)
    state = sup.State(slots=4, launches={0: running}, last_repo="o/a")
    for now in (0.0, 2.0, 4.0):
        sup.tick(NoGitHub(), state, ["o/a", "o/b"], now)
    assert [(repo, standby) for repo, _, standby in started] == [("o/a", True), ("o/b", True)]


def test_a_second_standby_for_one_repo_gives_way_to_a_newly_wanted_one(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 2)
    stopped: list[int] = []
    monkeypatch.setattr(vms, "stop_if_still_idle", lambda gh, run, why: stopped.append(run.slot))
    first, second, running = _launch(0, True), _launch(1, True), _launch(2, False)
    first.repo, second.repo, running.repo = "o/a", "o/a", "o/b"
    second.started = 1.0
    queue(jobs=0, free_mib=1 << 20)
    state = sup.State(slots=4, launches={0: first, 1: second, 2: running}, last_repo="o/a")
    sup.tick(NoGitHub(), state, ["o/a", "o/b"], 2.0)
    assert stopped == [1]
    assert started == []  # o/b's boots once the stopped one has gone


def test_a_build_standby_gives_way_to_another_labels_job_with_no_free_slot(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 2)
    stopped: list[str] = []
    monkeypatch.setattr(
        vms, "stop_if_still_idle", lambda gh, run, why: stopped.append(f"{run.repo} {why}")
    )
    standby = _launch(0, True)
    standby.label = "fc-cache-o--x"
    queue(jobs=1, free_mib=1 << 20)
    state = sup.State(slots=1, launches={0: standby})
    sup.tick(NoGitHub(), state, ["o/ok", "octo/agent"], 0.0)
    assert stopped == ["octo/agent is in the way of a queued job"]
    assert started == []  # the job takes the slot once the standby has gone


@pytest.mark.parametrize("busy", [True, False])
def test_a_starting_vm_covers_a_queued_job_only_until_its_runner_takes_one(
    pools: Pools, monkeypatch: pytest.MonkeyPatch, busy: bool
) -> None:
    queue, started = pools
    queue(build=1)
    label, slot = "", 0
    asked: list[str] = []

    def has_job(gh: github_api.GitHubApi, run: vms.Launch) -> bool:
        asked.append(run.runner_name)
        return busy

    monkeypatch.setattr(vms, "has_job", has_job)
    state = sup.State(slots=2)
    # Started a minute ago for the same repo and label.
    state.launches[slot] = vms.Launch(
        slot,
        "o/ok",
        slot,
        f"fc-{slot}",
        0.0,
        Path("m"),
        label=label,
    )
    sup.tick(NoGitHub(), state, ["o/ok"], 60.0)
    if busy:  # it has its own job, so the queued one gets a VM of its own
        assert len(started) == 1 and started[0][1] == label and state.launches[slot].busy
    else:  # its runner is still connecting and will take the queued job: no duplicate
        assert started == []
    assert asked == [f"fc-{slot}"]


def test_an_ended_vms_failed_bookkeeping_frees_its_slot_and_the_queued_job_still_starts(
    vm_records: Path, monkeypatch: pytest.MonkeyPatch, supervisor_log: list[str]
) -> None:
    ended = vms.Launch(0, "o/ok", 7, "fc-0-1", 0.0, vm_records / "metadata")
    vms.write_status(ended.record, vms.record(ended))

    def count_memory(run: vms.Launch) -> None:
        raise KeyError("deploy")

    monkeypatch.setattr(vms, "count_memory", count_memory)
    removed: list[str] = []
    monkeypatch.setattr(
        github_api, "delete_runner", lambda gh, repo, rid, name: removed.append(name)
    )
    monkeypatch.setattr(vms, "running_vms", lambda: set())
    for name in ("check_served", "only_colin"):
        monkeypatch.setattr(github_api, name, lambda *a, **k: True)
    monkeypatch.setattr(cache_disks, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(vms, "has_job", lambda gh, run: False)
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    job = github_api.Job("o/ok", 1, "j", tuple(github_api.RUNNER_LABELS))
    monkeypatch.setattr(
        github_api, "find_queued", lambda gh, repo, **_: [job] if repo == "o/ok" else []
    )
    started: list[int] = []

    def launch(
        gh: github_api.GitHubApi, state: sup.State, repo: str, slot: int, now: float, **k: object
    ) -> None:
        started.append(slot)
        state.launches[slot] = _launch(slot, False)

    monkeypatch.setattr(vms, "launch", launch)
    state = sup.State(slots=1, launches={0: ended})

    sup.tick(NoGitHub(), state, ["o/ok"], 10.0)

    assert started == [0] and state.launches[0] is not ended
    assert removed == ["fc-0-1"]  # the rest of its bookkeeping still ran
    assert "slot 0: fc-0-1 bookkeeping failed: KeyError: 'deploy'" in supervisor_log


@pytest.mark.parametrize(
    ("build_waited", "first"),
    [
        (60.0, "fc-cache-o--review"),  # the started run's review goes first
        (sup.FIRST_BUILD_WAIT_MAX + 60, "fc-cache-o--build"),  # the build waited too long
    ],
)
def test_a_started_runs_review_takes_the_slot_before_an_older_new_build(
    pools: Pools, monkeypatch: pytest.MonkeyPatch, build_waited: float, first: str
) -> None:
    _, started = pools
    now = 10_000.0
    labels = tuple(github_api.RUNNER_LABELS)
    jobs = [
        github_api.Job(
            "o/ok", 1, "build", (*labels, "fc-cache-o--build"), queued_at=now - build_waited
        ),
        github_api.Job("o/ok", 2, "review", (*labels, "fc-cache-o--review"), run_started=True),
    ]
    monkeypatch.setattr(github_api, "find_queued", lambda gh, repo, **_: jobs)

    sup.tick(NoGitHub(), sup.State(slots=1), ["o/ok"], now)

    assert started == [(0, first)]


def test_each_vm_a_poll_starts_is_told_the_hosts_last_load_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Registers(NoGitHub):
        def request(self, method: str, path: str, body: object = None) -> dict[str, Any]:
            return {"runner": {"id": 1}, "encoded_jit_config": "x"}

    record = {"slots": 5, "passed": True, "line": "PASS 5 slots"}
    load_testing.LOAD_TEST_FILE.write_text(json.dumps(record))
    monkeypatch.setattr(vms, "STATE_DIR", tmp_path)
    monkeypatch.setattr(vms, "LAST_REPO_FILE", tmp_path / "last-repo")
    for name in ("check_served", "only_colin"):
        monkeypatch.setattr(github_api, name, lambda *a, **k: True)
    job = github_api.Job("o/ok", 1, "j", tuple(github_api.RUNNER_LABELS))
    monkeypatch.setattr(github_api, "find_queued", lambda gh, repo, **_: [job])
    monkeypatch.setattr(cache_disks, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(vms, "has_job", lambda gh, run: False)
    monkeypatch.setattr(sup, "standby_count", lambda: 1)

    sup.tick(Registers(), sup.State(slots=2), ["o/ok"], 0.0)

    for slot in (0, 1):  # the job's VM, then the standby
        metadata = json.loads((tmp_path / f"metadata-slot{slot}.json").read_text())
        assert metadata["load_test"] == record


def test_its_modules_never_import_it_while_it_runs() -> None:
    """It runs as __main__: a module importing it would load a second copy, with its own State
    class, beside the running one. Its type is imported only for type checks."""
    for name in install_host.SUPERVISOR_MODULES:
        tree = ast.parse((Path(__file__).parent / name).read_text(encoding="utf-8"))
        checked = {
            id(n)
            for node in ast.walk(tree)
            if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
            for n in ast.walk(node)
        }
        for node in ast.walk(tree):
            if id(node) in checked:
                continue
            if isinstance(node, ast.Import):
                assert "ci_supervisor" not in [a.name for a in node.names], name
            if isinstance(node, ast.ImportFrom):
                assert node.module != "ci_supervisor", name
