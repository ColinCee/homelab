"""Tests for the agent service."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import _implement_status, _review_status, _run_implement, _run_review, app
from metrics import METRICS_REGISTRY, reset_metrics


def _client():
    return TestClient(app)


def _metric_value(name: str, labels: dict[str, str]) -> float:
    value = METRICS_REGISTRY.get_sample_value(name, labels)
    assert value is not None
    return value


@pytest.fixture(autouse=True)
def reset_state():
    reset_metrics()
    _review_status.clear()
    _implement_status.clear()
    yield
    reset_metrics()
    _review_status.clear()
    _implement_status.clear()


def test_health():
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@patch("main.reap_old_worktrees", new_callable=AsyncMock)
def test_startup_reaps_old_worktrees(mock_reap):
    with TestClient(app):
        pass

    mock_reap.assert_awaited_once()


def test_metrics_endpoint_exposes_prometheus_text():
    resp = _client().get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "# HELP agent_task_total" in resp.text
    assert 'agent_task_in_progress{task_type="review"} 0.0' in resp.text


@patch("main.review_pr", new_callable=AsyncMock)
def test_review_returns_202_accepted(mock_review):
    mock_review.return_value = {"model": "gpt-5.4", "elapsed_seconds": 1.5}
    resp = _client().post(
        "/review",
        json={"repo": "user/repo", "pr_number": 1},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["pr_number"] == 1


def test_review_missing_fields():
    resp = _client().post("/review", json={"pr_number": 1})
    assert resp.status_code == 422


def test_review_status_not_found():
    resp = _client().get("/review/99999")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_found"


@patch("main.review_pr", new_callable=AsyncMock)
def test_review_rejects_duplicate_in_flight(mock_review):
    """A second review for the same PR while one is in-flight should be rejected."""
    from main import _review_status

    _review_status["user/repo#42"] = {
        "status": "in_progress",
        "repo": "user/repo",
        "pr_number": 42,
    }
    try:
        resp = _client().post(
            "/review",
            json={"repo": "user/repo", "pr_number": 42},
        )
        assert resp.status_code == 409
        assert resp.json()["status"] == "already_in_progress"
        mock_review.assert_not_called()
    finally:
        _review_status.pop("user/repo#42", None)


# --- Implement endpoint tests ---


@patch("main.implement_issue", new_callable=AsyncMock)
def test_implement_returns_202_accepted(mock_impl):
    mock_impl.return_value = {"pr_number": 99, "elapsed_seconds": 5.0}
    resp = _client().post(
        "/implement",
        json={"repo": "user/repo", "issue_number": 10},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["issue_number"] == 10


def test_implement_missing_fields():
    resp = _client().post("/implement", json={"issue_number": 1})
    assert resp.status_code == 422


def test_implement_status_not_found():
    resp = _client().get("/implement/99999")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_found"


@patch("main.implement_issue", new_callable=AsyncMock)
def test_implement_rejects_duplicate_in_flight(mock_impl):
    from main import _implement_status

    _implement_status["user/repo#10"] = {
        "status": "in_progress",
        "repo": "user/repo",
        "issue_number": 10,
    }
    try:
        resp = _client().post(
            "/implement",
            json={"repo": "user/repo", "issue_number": 10},
        )
        assert resp.status_code == 409
        assert resp.json()["status"] == "already_in_progress"
        mock_impl.assert_not_called()
    finally:
        _implement_status.pop("user/repo#10", None)


def test_review_metrics_track_task_lifecycle():
    started = asyncio.Event()
    finish = asyncio.Event()

    async def fake_review_pr(**_kwargs) -> dict[str, object]:
        started.set()
        await finish.wait()
        return {"premium_requests": 2}

    async def run() -> None:
        with patch("main.review_pr", new=AsyncMock(side_effect=fake_review_pr)):
            task = asyncio.create_task(
                _run_review(
                    repo="user/repo",
                    pr_number=101,
                    model="gpt-5.4",
                    reasoning_effort="high",
                )
            )
            await started.wait()
            assert _metric_value("agent_task_in_progress", {"task_type": "review"}) == 1.0
            finish.set()
            await task

    asyncio.run(run())

    labels = {"task_type": "review", "status": "complete"}
    assert _metric_value("agent_task_in_progress", {"task_type": "review"}) == 0.0
    assert _metric_value("agent_task_total", labels) == 1.0
    assert _metric_value("agent_premium_requests_total", {"task_type": "review"}) == 2.0
    assert _metric_value("agent_task_duration_seconds_count", labels) == 1.0


@patch("main.implement_issue", new_callable=AsyncMock)
def test_implement_metrics_record_partial_status(mock_implement):
    mock_implement.return_value = {"status": "partial", "premium_requests": 3}

    asyncio.run(
        _run_implement(
            repo="user/repo",
            issue_number=202,
            model="gpt-5.4",
            reasoning_effort="high",
        )
    )

    labels = {"task_type": "implement", "status": "partial"}
    assert _metric_value("agent_task_in_progress", {"task_type": "implement"}) == 0.0
    assert _metric_value("agent_task_total", labels) == 1.0
    assert _metric_value("agent_premium_requests_total", {"task_type": "implement"}) == 3.0
    assert _metric_value("agent_task_duration_seconds_count", labels) == 1.0


@patch("main.implement_issue", new_callable=AsyncMock)
def test_implement_metrics_max_iterations_records_partial(mock_implement):
    mock_implement.return_value = {"status": "max_iterations", "premium_requests": 6}

    asyncio.run(
        _run_implement(
            repo="user/repo",
            issue_number=303,
            model="gpt-5.4",
            reasoning_effort="high",
        )
    )

    labels = {"task_type": "implement", "status": "partial"}
    assert _metric_value("agent_task_total", labels) == 1.0
    assert _metric_value("agent_premium_requests_total", {"task_type": "implement"}) == 6.0


@patch("main.implement_issue", new_callable=AsyncMock)
def test_implement_status_preserves_merge_details(mock_implement):
    mock_implement.return_value = {
        "status": "complete",
        "merged": True,
        "merge_commit_sha": "merge123",
        "merge_method": "squash",
        "premium_requests": 4,
    }

    asyncio.run(
        _run_implement(
            repo="user/repo",
            issue_number=404,
            model="gpt-5.4",
            reasoning_effort="high",
        )
    )

    status = _implement_status["user/repo#404"]
    assert status["status"] == "complete"
    assert status["merged"] is True
    assert status["merge_commit_sha"] == "merge123"
    assert status["merge_method"] == "squash"


# --- Fire-and-forget safety: error comments on failures ---


@patch("main.comment_on_issue", new_callable=AsyncMock)
@patch("main.review_pr", new_callable=AsyncMock)
def test_review_failure_posts_error_comment(mock_review, mock_comment):
    """When review_pr raises, _run_review posts an error comment on the PR."""
    from copilot import TaskError

    mock_review.side_effect = TaskError("CLI timed out", premium_requests=3)

    asyncio.run(
        _run_review(repo="user/repo", pr_number=42, model="gpt-5.4", reasoning_effort="high")
    )

    mock_comment.assert_called_once()
    args = mock_comment.call_args
    assert args[0][0] == "user/repo"
    assert args[0][1] == 42
    assert "Review failed" in args[0][2]


@patch("main.comment_on_issue", new_callable=AsyncMock)
@patch("main.review_pr", new_callable=AsyncMock)
def test_review_failure_skips_comment_when_already_commented(mock_review, mock_comment):
    """When TaskError has commented=True, _run_review does not double-post."""
    from copilot import TaskError

    mock_review.side_effect = TaskError("parse error", premium_requests=1, commented=True)

    asyncio.run(
        _run_review(repo="user/repo", pr_number=42, model="gpt-5.4", reasoning_effort="high")
    )

    mock_comment.assert_not_called()


@patch("main.comment_on_issue", new_callable=AsyncMock)
@patch("main.review_pr", new_callable=AsyncMock)
def test_review_unexpected_failure_posts_generic_comment(mock_review, mock_comment):
    """Non-TaskError exceptions also get an error comment."""
    mock_review.side_effect = RuntimeError("unexpected")

    asyncio.run(
        _run_review(repo="user/repo", pr_number=42, model="gpt-5.4", reasoning_effort="high")
    )

    mock_comment.assert_called_once()
    assert "see agent logs" in mock_comment.call_args[0][2]


@patch("main.comment_on_issue", new_callable=AsyncMock)
@patch("main.implement_issue", new_callable=AsyncMock)
def test_implement_failure_posts_error_comment(mock_impl, mock_comment):
    """When implement_issue raises, _run_implement posts an error comment."""
    from copilot import TaskError

    mock_impl.side_effect = TaskError("CLI crashed", premium_requests=5)

    asyncio.run(
        _run_implement(repo="user/repo", issue_number=10, model="gpt-5.4", reasoning_effort="high")
    )

    mock_comment.assert_called_once()
    assert mock_comment.call_args[0][1] == 10
    assert "Implementation failed" in mock_comment.call_args[0][2]


@patch("main.comment_on_issue", new_callable=AsyncMock)
@patch("main.implement_issue", new_callable=AsyncMock)
def test_implement_rejection_does_not_post_comment(mock_impl, mock_comment):
    """ValueError (untrusted author) should not post a comment on the issue."""
    mock_impl.side_effect = ValueError("untrusted author")

    asyncio.run(
        _run_implement(repo="user/repo", issue_number=10, model="gpt-5.4", reasoning_effort="high")
    )

    mock_comment.assert_not_called()
    assert _implement_status["user/repo#10"]["status"] == "rejected"
