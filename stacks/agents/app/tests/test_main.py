"""Tests for the agent service."""

import asyncio
from unittest.mock import AsyncMock, call, patch

import pytest
from fastapi.testclient import TestClient

from main import (
    ReviewRequest,
    _implement_status,
    _review_locks,
    _review_request_ids,
    _review_status,
    _review_tasks,
    _run_implement,
    _run_review,
    app,
    handle_review,
)
from metrics import METRICS_REGISTRY, reset_metrics

_ACTOR = "ColinCee"
_TOKEN = "ghs_test_token"


def _review_req(**kwargs) -> ReviewRequest:
    defaults = {
        "repo": "user/repo",
        "pr_number": 42,
        "triggered_by": _ACTOR,
        "github_token": _TOKEN,
    }
    return ReviewRequest(**(defaults | kwargs))


def _client():
    return TestClient(app)


def _metric_value(name: str, labels: dict[str, str]) -> float:
    value = METRICS_REGISTRY.get_sample_value(name, labels)
    assert value is not None
    return value


@pytest.fixture(autouse=True)
def reset_state():
    reset_metrics()
    _review_locks.clear()
    _review_request_ids.clear()
    _review_status.clear()
    _review_tasks.clear()
    _implement_status.clear()
    yield
    reset_metrics()
    _review_locks.clear()
    _review_request_ids.clear()
    _review_status.clear()
    _review_tasks.clear()
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


@patch("main.asyncio.create_task")
def test_review_returns_202_accepted(mock_create_task):
    async def run() -> None:
        task = asyncio.get_running_loop().create_task(asyncio.sleep(3600))

        def fake_create_task(coro):
            coro.close()
            return task

        mock_create_task.side_effect = fake_create_task

        result = await handle_review(_review_req(pr_number=1))

        assert result == {"status": "accepted", "pr_number": 1}
        assert _review_tasks["user/repo#1"] is task

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


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


def test_review_status_not_found():
    resp = _client().get("/review/99999")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_found"


def test_review_replaces_duplicate_in_flight():
    """A second review for the same PR should cancel the stale task and start fresh."""

    async def run() -> None:
        key = "user/repo#42"
        existing_task = asyncio.create_task(asyncio.sleep(3600))
        replacement_task = asyncio.create_task(asyncio.sleep(3600))

        _review_status[key] = {
            "status": "in_progress",
            "repo": "user/repo",
            "pr_number": 42,
        }
        _review_tasks[key] = existing_task

        def fake_create_task(coro):
            coro.close()
            return replacement_task

        with patch("main.asyncio.create_task", side_effect=fake_create_task) as mock_create_task:
            result = await handle_review(_review_req())

        assert result == {"status": "accepted", "pr_number": 42}
        assert existing_task.cancelled()
        assert _review_tasks[key] is replacement_task
        assert _review_status[key]["status"] == "in_progress"
        mock_create_task.assert_called_once()

        replacement_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await replacement_task

    asyncio.run(run())


def test_review_coalesces_concurrent_replacements():
    """Concurrent duplicate /review requests should only spawn one fresh task."""

    async def run() -> None:
        key = "user/repo#42"
        replacement_tasks: list[asyncio.Task[None]] = []
        cancellation_started = asyncio.Event()
        release_cancellation = asyncio.Event()

        async def stale_review() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_started.set()
                await release_cancellation.wait()
                raise

        _review_status[key] = {
            "status": "in_progress",
            "repo": "user/repo",
            "pr_number": 42,
        }
        existing_task = asyncio.create_task(stale_review())
        _review_tasks[key] = existing_task
        await asyncio.sleep(0)

        def fake_create_task(coro):
            coro.close()
            task = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
            replacement_tasks.append(task)
            return task

        with patch("main.asyncio.create_task", side_effect=fake_create_task):
            results_task = asyncio.gather(
                handle_review(_review_req()),
                handle_review(_review_req()),
            )
            await cancellation_started.wait()
            release_cancellation.set()
            results = await results_task

        assert results == [
            {"status": "accepted", "pr_number": 42},
            {"status": "accepted", "pr_number": 42},
        ]
        assert existing_task.cancelled()
        assert len(replacement_tasks) == 1
        assert _review_tasks[key] is replacement_tasks[0]

        for task in replacement_tasks:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())


# --- Implement endpoint tests ---


@patch("main.implement_issue", new_callable=AsyncMock)
def test_implement_returns_202_accepted(mock_impl):
    mock_impl.return_value = {"pr_number": 99, "elapsed_seconds": 5.0}
    resp = _client().post(
        "/implement",
        json={
            "repo": "user/repo",
            "issue_number": 10,
            "triggered_by": "ColinCee",
            "github_token": _TOKEN,
        },
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["issue_number"] == 10


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
            json={
                "repo": "user/repo",
                "issue_number": 10,
                "triggered_by": "ColinCee",
                "github_token": _TOKEN,
            },
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


@patch("main.update_comment", new_callable=AsyncMock)
@patch("main.comment_on_issue", new_callable=AsyncMock, return_value=1001)
@patch("main.find_issue_comment_by_body_prefix", new_callable=AsyncMock, return_value=None)
@patch("main.review_pr", new_callable=AsyncMock)
def test_review_updates_progress_comment_on_success(
    mock_review, mock_find_comment, mock_comment, mock_update
):
    mock_review.return_value = {"model": "gpt-5.4", "elapsed_seconds": 1.5}

    asyncio.run(
        _run_review(repo="user/repo", pr_number=42, model="gpt-5.4", reasoning_effort="high")
    )

    mock_find_comment.assert_awaited_once_with(
        "user/repo",
        42,
        "🔄 Review in progress for PR #",
    )
    mock_comment.assert_awaited_once_with("user/repo", 42, "🔄 Review in progress for PR #42...")
    mock_update.assert_awaited_once_with("user/repo", 1001, "✅ Review posted — see review above")


@patch("main.update_comment", new_callable=AsyncMock)
@patch("main.comment_on_issue", new_callable=AsyncMock)
@patch("main.find_issue_comment_by_body_prefix", new_callable=AsyncMock, return_value=2002)
@patch("main.review_pr", new_callable=AsyncMock)
def test_review_reuses_stale_progress_comment(
    mock_review, mock_find_comment, mock_comment, mock_update
):
    mock_review.return_value = {"model": "gpt-5.4", "elapsed_seconds": 1.5}

    asyncio.run(
        _run_review(repo="user/repo", pr_number=42, model="gpt-5.4", reasoning_effort="high")
    )

    mock_find_comment.assert_awaited_once_with(
        "user/repo",
        42,
        "🔄 Review in progress for PR #",
    )
    mock_comment.assert_not_called()
    assert mock_update.await_args_list == [
        call("user/repo", 2002, "🔄 Review in progress for PR #42..."),
        call("user/repo", 2002, "✅ Review posted — see review above"),
    ]


@patch("main.update_comment", new_callable=AsyncMock)
@patch("main.comment_on_issue", new_callable=AsyncMock)
@patch("main.find_issue_comment_by_body_prefix", new_callable=AsyncMock, return_value=2002)
def test_review_cancellation_updates_progress_comment(mock_find_comment, mock_comment, mock_update):
    started = asyncio.Event()

    async def fake_review_pr(**_kwargs):
        started.set()
        await asyncio.Future()

    async def run() -> None:
        key = "user/repo#42"
        with patch("main.review_pr", new=AsyncMock(side_effect=fake_review_pr)):
            task = asyncio.create_task(
                _run_review(
                    repo="user/repo",
                    pr_number=42,
                    model="gpt-5.4",
                    reasoning_effort="high",
                )
            )
            _review_tasks[key] = task
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())

    mock_find_comment.assert_awaited_once_with(
        "user/repo",
        42,
        "🔄 Review in progress for PR #",
    )
    mock_comment.assert_not_called()
    assert mock_update.await_args_list == [
        call("user/repo", 2002, "🔄 Review in progress for PR #42..."),
        call(
            "user/repo",
            2002,
            "⏹️ Review cancelled — superseded by newer /review request",
        ),
    ]
    assert "user/repo#42" not in _review_tasks
    assert _review_status["user/repo#42"]["status"] == "cancelled"


@patch("main.update_comment", new_callable=AsyncMock)
@patch("main.comment_on_issue", new_callable=AsyncMock, return_value=1001)
@patch("main.find_issue_comment_by_body_prefix", new_callable=AsyncMock, return_value=None)
@patch("main.review_pr", new_callable=AsyncMock)
def test_review_failure_posts_error_comment(
    mock_review, _mock_find_comment, mock_comment, mock_update
):
    """When review_pr raises, _run_review updates progress and posts an error comment."""
    from services.copilot import TaskError

    mock_review.side_effect = TaskError("CLI timed out", premium_requests=3)

    asyncio.run(
        _run_review(repo="user/repo", pr_number=42, model="gpt-5.4", reasoning_effort="high")
    )

    assert mock_comment.await_count == 2
    assert mock_comment.await_args_list[0] == call(
        "user/repo", 42, "🔄 Review in progress for PR #42..."
    )
    assert mock_comment.await_args_list[1] == call(
        "user/repo", 42, "⚠️ **Review failed** — CLI timed out"
    )
    mock_update.assert_awaited_once_with("user/repo", 1001, "⚠️ Review failed — CLI timed out")


@patch("main.update_comment", new_callable=AsyncMock)
@patch("main.comment_on_issue", new_callable=AsyncMock, return_value=1001)
@patch("main.find_issue_comment_by_body_prefix", new_callable=AsyncMock, return_value=None)
@patch("main.review_pr", new_callable=AsyncMock)
def test_review_failure_skips_comment_when_already_commented(
    mock_review, _mock_find_comment, mock_comment, mock_update
):
    """When TaskError has commented=True, _run_review only updates the progress comment."""
    from services.copilot import TaskError

    mock_review.side_effect = TaskError("parse error", premium_requests=1, commented=True)

    asyncio.run(
        _run_review(repo="user/repo", pr_number=42, model="gpt-5.4", reasoning_effort="high")
    )

    mock_comment.assert_awaited_once_with("user/repo", 42, "🔄 Review in progress for PR #42...")
    mock_update.assert_awaited_once_with("user/repo", 1001, "⚠️ Review failed — parse error")


@patch("main.update_comment", new_callable=AsyncMock)
@patch("main.comment_on_issue", new_callable=AsyncMock, return_value=1001)
@patch("main.find_issue_comment_by_body_prefix", new_callable=AsyncMock, return_value=None)
@patch("main.review_pr", new_callable=AsyncMock)
def test_review_unexpected_failure_posts_generic_comment(
    mock_review, _mock_find_comment, mock_comment, mock_update
):
    """Non-TaskError exceptions also get an error comment."""
    mock_review.side_effect = RuntimeError("unexpected")

    asyncio.run(
        _run_review(repo="user/repo", pr_number=42, model="gpt-5.4", reasoning_effort="high")
    )

    assert mock_comment.await_count == 2
    assert mock_comment.await_args_list[0] == call(
        "user/repo", 42, "🔄 Review in progress for PR #42..."
    )
    assert "see agent logs" in mock_comment.await_args_list[1].args[2]
    mock_update.assert_awaited_once_with(
        "user/repo", 1001, "⚠️ Review failed — see agent logs for details."
    )


@patch("main.update_comment", new_callable=AsyncMock)
@patch("main.comment_on_issue", new_callable=AsyncMock, return_value=3003)
@patch("main.find_issue_comment_by_body_prefix", new_callable=AsyncMock, return_value=None)
@patch("main._get_issue_for_progress", new_callable=AsyncMock, return_value={"title": "x"})
@patch("main.implement_issue", new_callable=AsyncMock)
def test_implement_updates_progress_comment_on_success(
    mock_impl, _mock_issue, mock_find_comment, mock_comment, mock_update
):
    mock_impl.return_value = {
        "status": "complete",
        "pr_number": 99,
        "pr_url": "https://github.com/user/repo/pull/99",
    }

    asyncio.run(
        _run_implement(repo="user/repo", issue_number=10, model="gpt-5.4", reasoning_effort="high")
    )

    mock_find_comment.assert_awaited_once_with("user/repo", 10, "🔄 Implementing #")
    mock_comment.assert_awaited_once_with("user/repo", 10, "🔄 Implementing #10...")
    mock_update.assert_awaited_once_with(
        "user/repo",
        3003,
        "✅ PR #99 created — https://github.com/user/repo/pull/99",
    )


@patch("main.update_comment", new_callable=AsyncMock)
@patch("main.comment_on_issue", new_callable=AsyncMock)
@patch("main.find_issue_comment_by_body_prefix", new_callable=AsyncMock, return_value=4004)
@patch("main._get_issue_for_progress", new_callable=AsyncMock, return_value={"title": "x"})
@patch("main.implement_issue", new_callable=AsyncMock)
def test_implement_reuses_stale_progress_comment(
    mock_impl, _mock_issue, mock_find_comment, mock_comment, mock_update
):
    mock_impl.return_value = {
        "status": "complete",
        "pr_number": 99,
        "pr_url": "https://github.com/user/repo/pull/99",
    }

    asyncio.run(
        _run_implement(repo="user/repo", issue_number=10, model="gpt-5.4", reasoning_effort="high")
    )

    mock_find_comment.assert_awaited_once_with("user/repo", 10, "🔄 Implementing #")
    mock_comment.assert_not_called()
    assert mock_update.await_args_list == [
        call("user/repo", 4004, "🔄 Implementing #10..."),
        call("user/repo", 4004, "✅ PR #99 created — https://github.com/user/repo/pull/99"),
    ]


@patch("main.update_comment", new_callable=AsyncMock)
@patch("main.comment_on_issue", new_callable=AsyncMock, return_value=3003)
@patch("main.find_issue_comment_by_body_prefix", new_callable=AsyncMock, return_value=None)
@patch("main._get_issue_for_progress", new_callable=AsyncMock, return_value={"title": "x"})
@patch("main.implement_issue", new_callable=AsyncMock)
def test_implement_failure_posts_error_comment(
    mock_impl, _mock_issue, _mock_find_comment, mock_comment, mock_update
):
    """When implement_issue raises, _run_implement updates progress and posts an error comment."""
    from services.copilot import TaskError

    mock_impl.side_effect = TaskError("CLI crashed", premium_requests=5)

    asyncio.run(
        _run_implement(repo="user/repo", issue_number=10, model="gpt-5.4", reasoning_effort="high")
    )

    assert mock_comment.await_count == 2
    assert mock_comment.await_args_list[0] == call("user/repo", 10, "🔄 Implementing #10...")
    assert "Implementation failed" in mock_comment.await_args_list[1].args[2]
    mock_update.assert_awaited_once_with("user/repo", 3003, "⚠️ Implementation failed — CLI crashed")


@patch("main.comment_on_issue", new_callable=AsyncMock)
@patch("main._get_issue_for_progress", new_callable=AsyncMock)
@patch("main.implement_issue", new_callable=AsyncMock)
def test_implement_content_rejection_does_not_post_comment(mock_impl, mock_issue, mock_comment):
    """Content trust rejection should not post a progress or error comment."""
    mock_issue.return_value = {"title": "x"}
    mock_impl.side_effect = ValueError("not trusted")

    asyncio.run(
        _run_implement(repo="user/repo", issue_number=10, model="gpt-5.4", reasoning_effort="high")
    )

    mock_comment.assert_not_called()
    assert _implement_status["user/repo#10"]["status"] == "rejected"
