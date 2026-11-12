"""The Beelink's own load test: when update.sh starts it, the drain around it, and its result."""

import json
from pathlib import Path

import pytest

from scripts import ci_supervisor as sup
from tests.conftest import NoGitHub

# The commands the code under test ran through subprocess.run (systemctl), never run for real.
CALLS: list[list[str]] = []


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A host with 5 slots, a load test that passes (and checks the drain is on while it
    runs), no VMs running, and the systemctl calls recorded in host/systemctl."""
    monkeypatch.setattr(sup, "DRAIN_FILE", tmp_path / "run" / "load-test-drain")
    monkeypatch.setattr(sup, "LOAD_TEST_FILE", tmp_path / "load-test.json")
    monkeypatch.setattr(sup, "SLOTS_FILE", tmp_path / "slots")
    monkeypatch.setattr(sup, "COMMIT_FILE", tmp_path / "commit")
    monkeypatch.setattr(sup, "UPDATE_LOCK", tmp_path / "update.lock")
    monkeypatch.setattr(sup, "LOAD_TEST_SCRIPT", tmp_path / "test_load.py")
    monkeypatch.setattr(sup, "log", lambda message: None)
    monkeypatch.setattr(sup, "running_vms", lambda: set())
    (tmp_path / "slots").write_text("5\n")
    (tmp_path / "commit").write_text("a" * 40 + "\n")
    write_test_script(tmp_path, 'echo "PASS $SLOTS slots: every VM booted and finished"')
    CALLS.clear()

    def run(args: list[str], **kwargs: object) -> sup.subprocess.CompletedProcess[str]:
        CALLS.append(args)
        return sup.subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sup.subprocess, "run", run)
    return tmp_path


def write_test_script(folder: Path, result: str) -> None:
    script = folder / "test_load.py"
    drain = folder / "run" / "load-test-drain"
    script.write_text(
        f'#!/bin/sh\ntest -e {drain} || {{ echo "not drained"; exit 3; }}\n{result}\n'
    )
    script.chmod(0o755)


def started(host: Path) -> bool:
    sup.start_load_test_if_due()
    return ["systemctl", "start", "--no-block", sup.LOAD_TEST_UNIT] in CALLS


def test_no_result_for_the_current_slot_count_starts_the_load_test(host: Path) -> None:
    assert started(host)


def test_a_start_refused_by_its_start_limit_is_left_for_a_later_pass(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sup.subprocess, "run", lambda args, **kwargs: sup.subprocess.CompletedProcess(args, 1)
    )
    assert sup.start_load_test_if_due() == 0


def test_a_pass_for_the_same_slots_and_test_does_not_start_it_again(host: Path) -> None:
    assert sup.run_load_test() == 0
    record = json.loads((host / "load-test.json").read_text())
    assert record["slots"] == 5 and record["passed"] is True and record["commit"] == "a" * 40
    assert record["line"] == "PASS 5 slots: every VM booted and finished"
    assert not started(host)


@pytest.mark.parametrize("change", ["slots", "test"])
def test_a_new_slot_count_or_a_changed_test_starts_it_again(host: Path, change: str) -> None:
    sup.run_load_test()
    if change == "slots":
        (host / "slots").write_text("4\n")
    else:
        write_test_script(host, 'echo "PASS $SLOTS slots: changed"')
    assert started(host)


def test_a_fail_lowers_the_count_by_one_and_the_lower_count_is_tested_next(host: Path) -> None:
    write_test_script(host, 'echo "FAIL $SLOTS slots: loaded Grafana: 1.4s over 1s"; exit 1')
    assert sup.run_load_test() == 1
    assert (host / "slots").read_text() == "4\n"
    assert json.loads((host / "load-test.json").read_text())["passed"] is False
    assert started(host)


def test_a_fail_never_lowers_the_count_below_three_nor_runs_again(host: Path) -> None:
    (host / "slots").write_text("3\n")
    write_test_script(host, 'echo "FAIL $SLOTS slots: slot 2: VM did not boot (exit 1)"; exit 1')
    sup.run_load_test()
    assert (host / "slots").read_text() == "3\n"
    assert not started(host)


@pytest.mark.parametrize(
    "broken",
    [
        "exit 2",  # no PASS or FAIL line
        None,  # the script cannot be started at all
    ],
)
def test_the_drain_ends_when_the_test_breaks_and_it_waits_to_try_again(
    host: Path, broken: str | None
) -> None:
    if broken is None:
        (host / "test_load.py").chmod(0o644)
        with pytest.raises(PermissionError):
            sup.run_load_test()
    else:
        write_test_script(host, broken)
        assert sup.run_load_test() == 1
    assert not sup.DRAIN_FILE.exists()
    record = json.loads((host / "load-test.json").read_text())
    assert record["ran"] is False and record["line"].startswith("not run:")
    assert not started(host)  # not drained again on the next update pass


def test_a_fail_kept_is_not_overwritten_when_lowering_the_count_breaks(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_test_script(host, 'echo "FAIL $SLOTS slots: slot 4: VM did not boot (exit 1)"; exit 1')
    monkeypatch.setattr(sup, "SLOTS_FILE", host / "no-such-folder" / "slots")
    with pytest.raises(FileNotFoundError):
        sup.run_load_test()
    record = json.loads((host / "load-test.json").read_text())
    assert record["ran"] is True and record["line"].startswith("FAIL 5 slots")


def test_the_drain_ends_when_jobs_outlast_its_limit_and_it_is_tried_again_later(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sup, "running_vms", lambda: {"homelab-ci-vm-0"})
    monkeypatch.setattr(sup, "DRAIN_LIMIT_SECONDS", 0)
    assert sup.run_load_test() == 1
    assert not sup.DRAIN_FILE.exists()
    record = json.loads((host / "load-test.json").read_text())
    assert record["line"] == "not run: 1 VMs still running after the drain limit"
    assert not started(host)  # not straight away: the jobs are still there
    monkeypatch.setattr(sup.time, "time", lambda: record["at"] + sup.LOAD_TEST_RETRY_SECONDS)
    assert started(host)


def test_the_unit_ends_the_drain_however_it_stops() -> None:
    unit = Path("ci-runner/systemd/homelab-ci-load-test.service").read_text()
    assert f"ExecStopPost=/usr/bin/rm -f {sup.DRAIN_FILE}" in unit


def queued_job(monkeypatch: pytest.MonkeyPatch, repo: str = "o/ok") -> None:
    monkeypatch.setattr(sup, "check_served", lambda *a, **k: True)
    monkeypatch.setattr(sup, "only_colin", lambda *a, **k: True)
    monkeypatch.setattr(sup, "reap", lambda *a, **k: None)
    monkeypatch.setattr(sup, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(sup, "mem_available_mib", lambda: 1 << 20)
    monkeypatch.setattr(sup, "disk_wait_percent", lambda: 0.0)
    monkeypatch.setattr(sup, "WAITS_FILE", sup.DRAIN_FILE.parent / "waits.jsonl")
    monkeypatch.setattr(sup, "WAIT_METRICS", sup.DRAIN_FILE.parent / "ci-waits.prom")
    monkeypatch.setattr(
        sup, "find_queued", lambda gh, r, **_: [sup.Job(r, 1, "j", tuple(sup.RUNNER_LABELS))]
    )


def test_while_drained_no_vm_starts_and_the_standby_stops(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued_job(monkeypatch)
    starts: list[int] = []
    monkeypatch.setattr(sup, "launch", lambda gh, state, repo, slot, now, **k: starts.append(slot))
    stopped: list[str] = []
    monkeypatch.setattr(sup, "stop_if_still_idle", lambda gh, run, why: stopped.append(why))
    standby = sup.Launch(0, "o/ok", 0, "fc-0", 0.0, Path("m"), True)
    state = sup.State(slots=5, launches={0: standby})
    sup.DRAIN_FILE.parent.mkdir(parents=True)
    sup.DRAIN_FILE.touch()

    sup.tick(NoGitHub(), state, ["o/ok"], 0.0)

    assert starts == [] and stopped == ["gives way to the host's load test"]
    assert state.waits[1].why == "the host's load test"
    assert sup.status(state, 0.0, github_ok=True, mem_available=0)["draining"] is True


def test_after_the_drain_the_supervisor_takes_the_lowered_count_and_jobs_again(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued_job(monkeypatch)
    starts: list[int] = []
    monkeypatch.setattr(sup, "launch", lambda gh, state, repo, slot, now, **k: starts.append(slot))
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    state = sup.State(slots=5, draining=True)
    (host / "slots").write_text("4\n")

    sup.tick(NoGitHub(), state, ["o/ok"], 0.0)

    assert state.slots == 4 and not state.draining and starts == [0]


def test_the_last_result_reads_back_as_written(host: Path) -> None:
    sup.run_load_test()
    assert sup.read_load_test() == json.loads((host / "load-test.json").read_text())
