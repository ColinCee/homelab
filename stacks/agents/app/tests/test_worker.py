"""Tests for the ephemeral worker entrypoint."""

import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import pytest


@patch.dict(
    os.environ,
    {
        "WORKER_TASK": "implement",
        "WORKER_REPO": "user/repo",
        "WORKER_ISSUE_NUMBER": "10",
        "GH_TOKEN": "ghs_test",
        "MODEL": "gpt-5.4",
        "REASONING_EFFORT": "high",
    },
)
@patch("worker.implement_issue", new_callable=AsyncMock)
@patch("worker.get_issue", new_callable=AsyncMock, return_value={"title": "test"})
@patch("worker.comment_on_issue", new_callable=AsyncMock, return_value=100)
@patch("worker.find_issue_comment_by_body_prefix", new_callable=AsyncMock, return_value=None)
@patch("worker.update_comment", new_callable=AsyncMock)
def test_implement_success_returns_zero(
    mock_update, mock_find, mock_comment, mock_issue, mock_impl, capsys
):
    mock_impl.return_value = {
        "status": "complete",
        "pr_number": 99,
        "pr_url": "https://github.com/user/repo/pull/99",
        "premium_requests": 5,
    }

    from worker import main

    exit_code = asyncio.run(main())

    assert exit_code == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    assert result["status"] == "complete"
    assert result["premium_requests"] == 5
    mock_impl.assert_awaited_once()


@patch.dict(
    os.environ,
    {
        "WORKER_TASK": "implement",
        "WORKER_REPO": "user/repo",
        "WORKER_ISSUE_NUMBER": "10",
        "GH_TOKEN": "ghs_test",
    },
)
@patch("worker.implement_issue", new_callable=AsyncMock)
@patch("worker.get_issue", new_callable=AsyncMock, return_value={"title": "test"})
@patch("worker.comment_on_issue", new_callable=AsyncMock, return_value=100)
@patch("worker.find_issue_comment_by_body_prefix", new_callable=AsyncMock, return_value=None)
@patch("worker.update_comment", new_callable=AsyncMock)
def test_implement_failure_returns_one(
    mock_update, mock_find, mock_comment, mock_issue, mock_impl, capsys
):
    mock_impl.return_value = {
        "status": "failed",
        "premium_requests": 2,
    }

    from worker import main

    exit_code = asyncio.run(main())

    assert exit_code == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    assert result["status"] == "failed"


@patch.dict(
    os.environ,
    {
        "WORKER_TASK": "implement",
        "WORKER_REPO": "user/repo",
        "WORKER_ISSUE_NUMBER": "10",
        "GH_TOKEN": "ghs_test",
    },
)
@patch("worker.implement_issue", new_callable=AsyncMock)
@patch("worker.get_issue", new_callable=AsyncMock, return_value={"title": "test"})
@patch("worker.comment_on_issue", new_callable=AsyncMock, return_value=100)
@patch("worker.find_issue_comment_by_body_prefix", new_callable=AsyncMock, return_value=None)
@patch("worker.update_comment", new_callable=AsyncMock)
def test_implement_exception_posts_error_comment(
    mock_update, mock_find, mock_comment, mock_issue, mock_impl, capsys
):
    from services.copilot import TaskError

    mock_impl.side_effect = TaskError("CLI crashed", premium_requests=3)

    from worker import main

    exit_code = asyncio.run(main())

    assert exit_code == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    assert result["status"] == "failed"
    assert result["premium_requests"] == 3

    # Should have posted error comment
    error_calls = [c for c in mock_comment.call_args_list if "failed" in str(c).lower()]
    assert len(error_calls) > 0


@patch.dict(
    os.environ,
    {
        "WORKER_TASK": "review",
        "WORKER_REPO": "user/repo",
        "WORKER_PR_NUMBER": "42",
        "GH_TOKEN": "ghs_test",
        "MODEL": "gpt-5.4",
        "REASONING_EFFORT": "high",
    },
)
@patch("worker.review_pr", new_callable=AsyncMock)
@patch("worker.comment_on_issue", new_callable=AsyncMock, return_value=100)
@patch("worker.find_issue_comment_by_body_prefix", new_callable=AsyncMock, return_value=None)
@patch("worker.update_comment", new_callable=AsyncMock)
def test_review_success_returns_zero(mock_update, mock_find, mock_comment, mock_review, capsys):
    mock_review.return_value = {
        "status": "complete",
        "premium_requests": 2,
    }

    from worker import main

    exit_code = asyncio.run(main())

    assert exit_code == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    assert result["status"] == "complete"
    mock_review.assert_awaited_once()


@patch.dict(
    os.environ,
    {
        "WORKER_TASK": "review",
        "WORKER_REPO": "user/repo",
        "WORKER_PR_NUMBER": "42",
        "GH_TOKEN": "ghs_test",
    },
)
@patch("worker.review_pr", new_callable=AsyncMock)
@patch("worker.comment_on_issue", new_callable=AsyncMock, return_value=100)
@patch("worker.find_issue_comment_by_body_prefix", new_callable=AsyncMock, return_value=None)
@patch("worker.update_comment", new_callable=AsyncMock)
def test_review_posts_progress_comment(mock_update, mock_find, mock_comment, mock_review):
    mock_review.return_value = {"status": "complete", "premium_requests": 1}

    from worker import main

    asyncio.run(main())

    # Should have posted a progress comment with the review prefix
    progress_calls = [c for c in mock_comment.call_args_list if "Review in progress" in str(c)]
    assert len(progress_calls) > 0


@patch.dict(
    os.environ,
    {
        "WORKER_TASK": "implement",
        "WORKER_REPO": "user/repo",
        "WORKER_ISSUE_NUMBER": "10",
        "GH_TOKEN": "ghs_test",
    },
)
@patch("worker.implement_issue", new_callable=AsyncMock)
@patch("worker.get_issue", new_callable=AsyncMock, return_value={"title": "test"})
@patch("worker.comment_on_issue", new_callable=AsyncMock, return_value=100)
@patch("worker.find_issue_comment_by_body_prefix", new_callable=AsyncMock, return_value=None)
@patch("worker.update_comment", new_callable=AsyncMock)
def test_implement_content_rejection_returns_one(
    mock_update, mock_find, mock_comment, mock_issue, mock_impl, capsys
):
    """Content trust rejection should not post error comments."""
    mock_impl.side_effect = ValueError("not trusted")

    from worker import main

    exit_code = asyncio.run(main())

    assert exit_code == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    assert result["status"] == "rejected"


def test_unknown_task_type_returns_one():
    with patch.dict(
        os.environ,
        {
            "WORKER_TASK": "unknown",
            "WORKER_REPO": "user/repo",
            "GH_TOKEN": "ghs_test",
        },
    ):
        from worker import main

        exit_code = asyncio.run(main())
        assert exit_code == 1


def test_missing_env_var_raises():
    with patch.dict(os.environ, {}, clear=True):
        from worker import _require_env

        with pytest.raises(RuntimeError, match="WORKER_TASK"):
            _require_env("WORKER_TASK")
