import json
from pathlib import Path

import ci_supervisor as sup
import job_waits
import pytest


@pytest.fixture(autouse=True, name="waits_file")
def fixture_waits_file(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    path = tmp_path_factory.mktemp("status") / "waits.jsonl"
    monkeypatch.setattr(job_waits, "WAITS_FILE", path)
    return path


@pytest.fixture(autouse=True, name="wait_metrics")
def fixture_wait_metrics(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    path = tmp_path_factory.mktemp("metrics") / "ci-waits.prom"
    monkeypatch.setattr(job_waits, "WAIT_METRICS", path)
    return path


def test_failed_polls_in_a_row_go_to_prometheus_and_a_good_poll_clears_them(
    wait_metrics: Path,
) -> None:
    state = sup.State()

    def metric(name: str) -> str:
        line = next(x for x in wait_metrics.read_text().splitlines() if x.startswith(name + " "))
        return line.split()[1]

    for now in (100.0, 102.0, 104.0):
        job_waits.note_poll(state, False, now)
    assert metric("homelab_ci_poll_failures") == "1"  # written when polls start failing
    job_waits.note_poll(state, False, 120.0)  # 15 s on: written again
    assert metric("homelab_ci_poll_failures") == "4"
    assert metric("homelab_ci_poll_failed_seconds") == "20"
    job_waits.note_poll(state, True, 122.0)  # written at once when polls work again
    assert metric("homelab_ci_poll_failures") == "0"
    assert metric("homelab_ci_poll_failed_seconds") == "0"
    assert state.poll_failed_since is None


def test_a_long_rest_keeps_showing_polls_failing(
    wait_metrics: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(job_waits.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(job_waits.time, "time", lambda: clock[0])
    monkeypatch.setattr(job_waits.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    state = sup.State()
    job_waits.note_poll(state, False, clock[0])  # rate limited
    job_waits.rest(state, 1800.0)
    assert clock[0] == 2800.0
    assert "homelab_ci_poll_failed_seconds 1800" in wait_metrics.read_text()


def test_an_unwritable_wait_metrics_file_is_logged_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supervisor_log: list[str]
) -> None:
    monkeypatch.setattr(job_waits, "WAIT_METRICS", tmp_path / "missing" / "ci-waits.prom")
    state = sup.State()
    for now in (0.0, 2.0):
        job_waits.note_waits(state, ["o/ok"], {}, now)
    assert len(supervisor_log) == 1 and supervisor_log[0].startswith("could not write")


def test_waits_over_30_days_old_are_pruned_once_an_hour(waits_file: Path) -> None:
    day = 24 * 3600.0
    now = 100 * day
    old = {"job": 1, "repo": "o/ok", "since": 0, "until": int(now - 31 * day)}
    waits_file.write_text(json.dumps(old) + "\nhalf a li")
    state = sup.State()
    job_waits.record_wait(state, 2, job_waits.Wait("o/ok", now - 5, seen=now), now)
    assert [json.loads(line)["job"] for line in waits_file.read_text().splitlines()] == [2]
    with waits_file.open("a") as f:
        f.write(json.dumps(old) + "\n")
    job_waits.record_wait(
        state, 3, job_waits.Wait("o/ok", now, seen=now), now + 60
    )  # pruned an hour ago
    assert [json.loads(line)["job"] for line in waits_file.read_text().splitlines()] == [2, 1, 3]


def test_an_unwritable_waits_file_is_logged_once_and_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supervisor_log: list[str]
) -> None:
    monkeypatch.setattr(job_waits, "WAITS_FILE", tmp_path / "missing" / "waits.jsonl")
    state = sup.State()
    for job in (1, 2):
        job_waits.record_wait(state, job, job_waits.Wait("o/ok", 0.0), 10.0)
    assert len(supervisor_log) == 1 and supervisor_log[0].startswith("could not write")
