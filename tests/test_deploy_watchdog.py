from datetime import UTC, datetime, timedelta
from typing import Any

from scripts import deploy_watchdog as watchdog

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
LIMIT = timedelta(minutes=15)


def _run(number: int, created: str, *, status: str = "queued", started: str | None = None):
    return {
        "run_number": number,
        "status": status,
        "created_at": created,
        "run_started_at": started,
        "html_url": f"https://example.test/runs/{number}",
    }


def test_flags_only_runs_queued_past_the_limit() -> None:
    runs = [
        _run(1, "2026-10-03T11:30:00Z"),
        _run(2, "2026-10-03T11:50:00Z"),
        _run(3, "2026-10-03T11:00:00Z", status="in_progress"),
    ]

    problems = watchdog.stalled_runs(runs, NOW, LIMIT)

    assert [p.summary for p in problems] == ["Deploy run #1 queued for 30 min"]
    assert problems[0].url == "https://example.test/runs/1"


def test_rerun_attempt_uses_its_own_start_time() -> None:
    runs = [_run(4, "2026-10-01T00:00:00Z", started="2026-10-03T11:55:00Z")]

    assert watchdog.stalled_runs(runs, NOW, LIMIT) == []


def test_notifies_only_on_state_transitions() -> None:
    problem = [watchdog.Problem("down")]

    assert watchdog.should_notify(problem, "success")
    assert watchdog.should_notify(problem, None)
    assert not watchdog.should_notify(problem, "failure")
    assert watchdog.should_notify([], "failure")
    assert not watchdog.should_notify([], "success")


def test_api_errors_become_problems() -> None:
    def broken(path: str, query: str = "") -> Any:
        raise OSError("boom")

    problems = watchdog.collect_problems(
        broken, "o/r", workflow="deploy.yaml", now=NOW, max_queued=LIMIT
    )

    assert [p.summary for p in problems] == ["Could not list queued deploy runs: boom"]


def test_message_lists_problems_with_links() -> None:
    message = watchdog.format_message(
        [watchdog.Problem("Deploy run #1 queued for 30 min", "https://x/1")], recovered=False
    )

    assert "- Deploy run #1 queued for 30 min (https://x/1)" in message
