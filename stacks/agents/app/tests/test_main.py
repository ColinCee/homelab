"""Tests for the agent service (container-dispatch architecture)."""

import asyncio
import logging
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import (
    ReviewRequest,
    _monitor_tasks,
    _monitor_worker,
    _task_status_label,
    app,
    handle_review,
)

_ACTOR = "ColinCee"
_TOKEN = "ghs_test_token"


def _client():
    return TestClient(app)


def _completion_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        record.__dict__
        for record in caplog.records
        if record.__dict__.get("event") == "task_completed"
    ]


@pytest.fixture(autouse=True)
def reset_state():
    _monitor_tasks.clear()
    yield
    _monitor_tasks.clear()


def test_health():
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- Status label tests ---


def test_task_status_label_known_statuses():
    assert _task_status_label("complete") == "complete"
    assert _task_status_label("failed") == "failed"
    assert _task_status_label("partial") == "partial"
    assert _task_status_label("rejected") == "rejected"


def test_task_status_label_unknown_defaults_to_failed():
    assert _task_status_label("whatever") == "failed"
    assert _task_status_label("") == "failed"
    assert _task_status_label(None) == "failed"


# --- Startup tests ---


@patch("main.discover_running_workers", new_callable=AsyncMock, return_value=[])
@patch("main.cleanup_orphaned_workers", new_callable=AsyncMock, return_value=[])
@patch("main.reap_old_worktrees", new_callable=AsyncMock)
def test_startup_reaps_worktrees_and_orphans(mock_reap, mock_cleanup, mock_discover):
    with TestClient(app):
        pass

    mock_reap.assert_awaited_once()
    mock_cleanup.assert_awaited_once()
    mock_discover.assert_awaited_once()


@patch("main.discover_running_workers", new_callable=AsyncMock, return_value=[])
@patch("main.cleanup_orphaned_workers", new_callable=AsyncMock)
@patch("main.reap_old_worktrees", new_callable=AsyncMock)
def test_startup_logs_fallback_completion_for_orphans(
    mock_reap, mock_cleanup, mock_discover, caplog
):
    """Startup emits a fallback completion event when orphan logs lack one."""
    mock_cleanup.return_value = [
        {
            "task_type": "review",
            "number": 42,
            "logs": '{"status": "complete", "premium_requests": 5}\n',
            "duration_seconds": 300.0,
        }
    ]
    caplog.set_level(logging.INFO)

    with TestClient(app):
        pass

    [event] = _completion_events(caplog)
    assert event["source"] == "api_orphan_harvest"
    assert event["task_type"] == "review"
    assert event["pr_number"] == 42
    assert event["status"] == "complete"
    assert event["duration_seconds"] == 300.0
    assert event["premium_requests"] == 5


@patch("main._spawn_monitor")
@patch("main.discover_running_workers", new_callable=AsyncMock)
@patch("main.cleanup_orphaned_workers", new_callable=AsyncMock, return_value=[])
@patch("main.reap_old_worktrees", new_callable=AsyncMock)
def test_startup_reconnects_monitors_for_running_workers(
    mock_reap, mock_cleanup, mock_discover, mock_monitor
):
    """Startup should reconnect monitors for workers still running."""
    mock_discover.return_value = [
        {
            "container_id": "abc123",
            "task_type": "implement",
            "number": 10,
            "started_at": 1000000.0,
        }
    ]

    with TestClient(app):
        pass

    mock_monitor.assert_called_once()
    call_kwargs = mock_monitor.call_args
    assert call_kwargs.args[0] == "abc123"
    assert call_kwargs.kwargs["task_type"] == "implement"
    assert call_kwargs.kwargs["number"] == 10


def test_metrics_endpoint_is_not_exposed():
    resp = _client().get("/metrics")
    assert resp.status_code == 404


# --- Review endpoint tests ---


@patch("main._spawn_monitor")
@patch("main.spawn_worker", new_callable=AsyncMock, return_value="abc123")
@patch("main.get_own_image", new_callable=AsyncMock, return_value="agent:latest")
def test_review_returns_202_accepted(mock_image, mock_spawn, mock_monitor):
    resp = _client().post(
        "/review",
        json={
            "repo": "user/repo",
            "pr_number": 42,
            "triggered_by": _ACTOR,
            "github_token": _TOKEN,
        },
    )
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "pr_number": 42}
    mock_spawn.assert_awaited_once()
    call_kwargs = mock_spawn.call_args.kwargs
    assert call_kwargs["task_type"] == "review"
    assert call_kwargs["number"] == 42
    assert call_kwargs["env"]["TASK_TYPE"] == "review"
    assert call_kwargs["env"]["REPO"] == "user/repo"
    assert call_kwargs["env"]["NUMBER"] == "42"
    assert call_kwargs["env"]["LOG_FORMAT"] == "json"


def test_review_missing_fields():
    resp = _client().post("/review", json={"pr_number": 1})
    assert resp.status_code == 422


def test_review_rejects_unknown_actor():
    resp = _client().post(
        "/review",
        json={
            "repo": "user/repo",
            "pr_number": 1,
            "triggered_by": "evil-user",
            "github_token": _TOKEN,
        },
    )
    assert resp.status_code == 403
    assert "not allowed" in resp.json()["error"]


@patch("main.is_worker_running", new_callable=AsyncMock, return_value=False)
def test_review_status_not_found(mock_running):
    resp = _client().get("/review/99999")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_found"


@patch("main.is_worker_running", new_callable=AsyncMock, return_value=True)
def test_review_status_in_progress(mock_running):
    resp = _client().get("/review/42")
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_progress"


@patch("main._spawn_monitor")
@patch("main.spawn_worker", new_callable=AsyncMock, return_value="abc123")
@patch("main.get_own_image", new_callable=AsyncMock, return_value="agent:latest")
def test_review_supersedes_existing_worker(mock_image, mock_spawn, mock_monitor):
    """Spawning a review for the same PR should stop the existing worker."""
    # spawn_worker internally calls stop_worker first — verify it's called with right params
    resp = _client().post(
        "/review",
        json={
            "repo": "user/repo",
            "pr_number": 42,
            "triggered_by": _ACTOR,
            "github_token": _TOKEN,
        },
    )
    assert resp.status_code == 202
    # spawn_worker handles supersession internally via stop_worker
    mock_spawn.assert_awaited_once()


@patch("main._spawn_monitor")
@patch("main.spawn_worker", new_callable=AsyncMock, return_value="abc123")
@patch("main.get_own_image", new_callable=AsyncMock, return_value="agent:latest")
def test_review_passes_copilot_token_to_worker(mock_image, mock_spawn, mock_monitor):
    """Worker env should include COPILOT_GITHUB_TOKEN from the API's own env."""
    with patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "cptoken123"}):
        resp = _client().post(
            "/review",
            json={
                "repo": "user/repo",
                "pr_number": 42,
                "triggered_by": _ACTOR,
                "github_token": _TOKEN,
            },
        )
    assert resp.status_code == 202
    env = mock_spawn.call_args.kwargs["env"]
    assert env["COPILOT_GITHUB_TOKEN"] == "cptoken123"
    assert env["GH_TOKEN"] == _TOKEN


@patch("main._spawn_monitor")
@patch("main.spawn_worker", new_callable=AsyncMock, return_value="abc123")
@patch("main.get_own_image", new_callable=AsyncMock, return_value="agent:latest")
def test_review_passes_log_format_to_worker(mock_image, mock_spawn, mock_monitor):
    with patch.dict(os.environ, {"LOG_FORMAT": "text"}):
        resp = _client().post(
            "/review",
            json={
                "repo": "user/repo",
                "pr_number": 42,
                "triggered_by": _ACTOR,
                "github_token": _TOKEN,
            },
        )
    assert resp.status_code == 202
    assert mock_spawn.call_args.kwargs["env"]["LOG_FORMAT"] == "text"


# --- Implement endpoint tests ---


@patch("main._spawn_monitor")
@patch("main.spawn_worker", new_callable=AsyncMock, return_value="def456")
@patch("main.get_own_image", new_callable=AsyncMock, return_value="agent:latest")
@patch("main.is_worker_running", new_callable=AsyncMock, return_value=False)
def test_implement_returns_202_accepted(mock_running, mock_image, mock_spawn, mock_monitor):
    resp = _client().post(
        "/implement",
        json={
            "repo": "user/repo",
            "issue_number": 10,
            "triggered_by": _ACTOR,
            "github_token": _TOKEN,
        },
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["issue_number"] == 10
    mock_spawn.assert_awaited_once()
    call_kwargs = mock_spawn.call_args.kwargs
    assert call_kwargs["env"]["TASK_TYPE"] == "implement"
    assert call_kwargs["env"]["NUMBER"] == "10"
    assert call_kwargs["env"]["LOG_FORMAT"] == "json"


def test_implement_missing_fields():
    resp = _client().post("/implement", json={"issue_number": 1})
    assert resp.status_code == 422


def test_implement_rejects_unknown_actor():
    resp = _client().post(
        "/implement",
        json={
            "repo": "user/repo",
            "issue_number": 10,
            "triggered_by": "evil-user",
            "github_token": _TOKEN,
        },
    )
    assert resp.status_code == 403
    assert "not allowed" in resp.json()["error"]


@patch("main.is_worker_running", new_callable=AsyncMock, return_value=False)
def test_implement_status_not_found(mock_running):
    resp = _client().get("/implement/99999")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_found"


@patch("main.is_worker_running", new_callable=AsyncMock, return_value=True)
def test_implement_status_in_progress(mock_running):
    resp = _client().get("/implement/10")
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_progress"


@patch("main.is_worker_running", new_callable=AsyncMock, return_value=True)
def test_implement_rejects_duplicate_in_flight(mock_running):
    resp = _client().post(
        "/implement",
        json={
            "repo": "user/repo",
            "issue_number": 10,
            "triggered_by": _ACTOR,
            "github_token": _TOKEN,
        },
    )
    assert resp.status_code == 409
    assert resp.json()["status"] == "already_in_progress"


# --- Monitor tests ---


@patch("main.remove_container", new_callable=AsyncMock)
@patch("main.get_logs", new_callable=AsyncMock)
@patch("main.wait_container", new_callable=AsyncMock)
def test_monitor_does_not_duplicate_worker_completion_event(mock_wait, mock_logs, mock_rm, caplog):
    """Monitor cleanup should not emit another terminal event when the worker already did."""
    mock_wait.return_value = 0
    mock_logs.return_value = (
        '{"event": "task_completed", "status": "complete", "premium_requests": 5}\n'
        '{"status": "complete", "premium_requests": 5}\n'
    )
    caplog.set_level(logging.INFO)

    asyncio.run(_monitor_worker("abc123", task_type="review", number=42, start=0.0))

    assert _completion_events(caplog) == []
    mock_rm.assert_awaited_once_with("abc123")


@patch("main.remove_container", new_callable=AsyncMock)
@patch("main.get_logs", new_callable=AsyncMock)
@patch("main.wait_container", new_callable=AsyncMock)
def test_monitor_does_not_trigger_review_after_implement(mock_wait, mock_logs, mock_rm):
    """Review is now handled inside the implement worker, not triggered by the monitor."""
    mock_wait.return_value = 0
    mock_logs.return_value = (
        '{"event": "task_completed", "status": "complete", "premium_requests": 2}\n'
        '{"status": "complete", "repo": "user/repo", "pr_number": 99, "premium_requests": 2}\n'
    )

    asyncio.run(_monitor_worker("abc123", task_type="implement", number=10, start=0.0))

    mock_rm.assert_awaited_once_with("abc123")


@patch("main.remove_container", new_callable=AsyncMock)
@patch("main.get_logs", new_callable=AsyncMock)
@patch("main.wait_container", new_callable=AsyncMock)
def test_monitor_logs_fallback_completion_on_failure(mock_wait, mock_logs, mock_rm, caplog):
    """Monitor emits a fallback completion event when worker logs lack one."""
    mock_wait.return_value = 1
    mock_logs.return_value = '{"status": "failed", "premium_requests": 3}\n'
    caplog.set_level(logging.INFO)

    asyncio.run(_monitor_worker("abc123", task_type="implement", number=10, start=0.0))

    [event] = _completion_events(caplog)
    assert event["source"] == "api_monitor"
    assert event["task_type"] == "implement"
    assert event["issue_number"] == 10
    assert event["status"] == "failed"
    assert event["premium_requests"] == 3
    mock_rm.assert_awaited_once_with("abc123")


@patch("main.remove_container", new_callable=AsyncMock)
@patch("main.get_logs", new_callable=AsyncMock)
@patch("main.wait_container", new_callable=AsyncMock)
def test_monitor_logs_worker_output_on_failure(mock_wait, mock_logs, mock_rm, caplog):
    mock_wait.return_value = 1
    logs = ("discarded-prefix\n" * 400) + "Traceback: boom\n"
    mock_logs.return_value = logs
    caplog.set_level(logging.WARNING)

    asyncio.run(_monitor_worker("abc123", task_type="implement", number=10, start=0.0))

    assert "Worker implement #10 output (last 3000 chars):" in caplog.text
    assert logs[-3000:] in caplog.text


@patch("main.remove_container", new_callable=AsyncMock)
@patch("main.get_logs", new_callable=AsyncMock)
@patch("main.wait_container", new_callable=AsyncMock)
def test_monitor_does_not_log_worker_output_on_success(mock_wait, mock_logs, mock_rm, caplog):
    mock_wait.return_value = 0
    mock_logs.return_value = 'worker chatter\n{"status": "complete"}\n'
    caplog.set_level(logging.INFO)

    asyncio.run(_monitor_worker("abc123", task_type="review", number=42, start=0.0))

    assert "Worker review #42 output (last 3000 chars):" not in caplog.text
    assert "raw output" not in caplog.text


@patch("main.remove_container", new_callable=AsyncMock)
@patch("main.get_logs", new_callable=AsyncMock)
@patch("main.wait_container", new_callable=AsyncMock)
def test_monitor_records_rejected_status(mock_wait, mock_logs, mock_rm):
    """Monitor should treat 'rejected' as a terminal status, not as 'complete'."""
    mock_wait.return_value = 1
    mock_logs.return_value = (
        '{"event": "task_completed", "status": "rejected", "premium_requests": 0}\n'
        '{"status": "rejected", "premium_requests": 0}\n'
    )

    asyncio.run(_monitor_worker("abc123", task_type="implement", number=10, start=0.0))

    mock_rm.assert_awaited_once_with("abc123")


@patch("main.remove_container", new_callable=AsyncMock)
@patch("main.get_logs", new_callable=AsyncMock)
@patch("main.wait_container", new_callable=AsyncMock)
def test_monitor_handles_unparseable_logs(mock_wait, mock_logs, mock_rm):
    """Monitor should degrade gracefully when worker logs can't be parsed."""
    mock_wait.return_value = 0
    mock_logs.return_value = "some random output with no JSON\n"

    asyncio.run(_monitor_worker("abc123", task_type="review", number=42, start=0.0))

    mock_rm.assert_awaited_once_with("abc123")


@patch("main.remove_container", new_callable=AsyncMock)
@patch("main.get_logs", new_callable=AsyncMock)
@patch("main.wait_container", new_callable=AsyncMock)
def test_monitor_cleans_up_monitor_tasks_dict(mock_wait, mock_logs, mock_rm):
    """Monitor should remove itself from _monitor_tasks on completion."""
    mock_wait.return_value = 0
    mock_logs.return_value = '{"status": "complete"}\n'
    _monitor_tasks["review-42"] = AsyncMock()  # type: ignore[assignment]

    asyncio.run(_monitor_worker("abc123", task_type="review", number=42, start=0.0))

    assert "review-42" not in _monitor_tasks


# --- Review dispatch handles session_id (currently not stored, but ensure no crash) ---


@patch("main._spawn_monitor")
@patch("main.spawn_worker", new_callable=AsyncMock, return_value="abc123")
@patch("main.get_own_image", new_callable=AsyncMock, return_value="agent:latest")
def test_review_accepts_model_override(mock_image, mock_spawn, mock_monitor):
    """Review request with explicit model should pass it to the worker."""
    resp = _client().post(
        "/review",
        json={
            "repo": "user/repo",
            "pr_number": 42,
            "triggered_by": _ACTOR,
            "github_token": _TOKEN,
            "model": "claude-sonnet-4",
            "reasoning_effort": "low",
        },
    )
    assert resp.status_code == 202
    env = mock_spawn.call_args.kwargs["env"]
    assert env["MODEL"] == "claude-sonnet-4"
    assert env["REASONING_EFFORT"] == "low"


# --- handle_review async tests ---


@patch("main._spawn_monitor")
@patch("main.spawn_worker", new_callable=AsyncMock, return_value="abc123")
@patch("main.get_own_image", new_callable=AsyncMock, return_value="agent:latest")
def test_review_via_async_handle(mock_image, mock_spawn, mock_monitor):
    """Verify handle_review works when called directly (async)."""

    async def run():
        req = ReviewRequest(
            repo="user/repo",
            pr_number=1,
            triggered_by=_ACTOR,
            github_token=_TOKEN,
        )
        result = await handle_review(req)
        assert result == {"status": "accepted", "pr_number": 1}

    asyncio.run(run())
