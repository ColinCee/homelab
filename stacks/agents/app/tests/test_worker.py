"""Tests for the ephemeral worker entrypoint."""

import asyncio
import os
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from conftest import IMPLEMENT_ENV, REVIEW_ENV

from models import GitHubIssue, TaskResult


def _implement_patches(mock_impl: AsyncMock):
    """Return context managers for implement worker tests."""
    return [
        patch("worker.implement_issue", mock_impl),
        patch("worker.get_issue", new_callable=AsyncMock, return_value=GitHubIssue(title="test")),
        patch("worker.safe_comment", new_callable=AsyncMock),
        patch("worker.comment_on_issue", new_callable=AsyncMock, return_value=100),
        patch(
            "worker.find_issue_comment_by_body_prefix",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("worker.update_comment", new_callable=AsyncMock),
    ]


def _review_patches(mock_review: AsyncMock):
    """Return context managers for review worker tests."""
    return [
        patch("worker.review_pr", mock_review),
        patch("worker.safe_comment", new_callable=AsyncMock),
        patch("worker.comment_on_issue", new_callable=AsyncMock, return_value=100),
        patch(
            "worker.find_issue_comment_by_body_prefix",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("worker.update_comment", new_callable=AsyncMock),
    ]


def _enter_patches(stack: ExitStack, patches, env: dict[str, str]):
    """Enter env dict patch + all mock patches on an ExitStack."""
    stack.enter_context(patch.dict(os.environ, env))
    for p in patches:
        stack.enter_context(p)


def test_implement_success_returns_zero(capsys):
    mock_impl = AsyncMock(
        return_value=TaskResult(
            status="complete",
            pr_number=99,
            pr_url="https://github.com/user/repo/pull/99",
            premium_requests=5,
            api_time_seconds=60,
            models={"gpt-5.4": "883.6k in, 17.7k out, 788.5k cached"},
            tokens_line="↑ 883.6k • ↓ 17.7k • 788.5k (cached)",
            session_id="sess-123",
        )
    )
    env = {**IMPLEMENT_ENV, "MODEL": "gpt-5.4", "REASONING_EFFORT": "high"}
    with ExitStack() as stack:
        _enter_patches(stack, _implement_patches(mock_impl), env)
        from worker import main

        exit_code = asyncio.run(main())

    assert exit_code == 0
    result = TaskResult.model_validate_json(capsys.readouterr().out.strip())
    assert result.status == "complete"
    assert result.premium_requests == 5
    assert result.api_time_seconds == 60
    assert result.models == {"gpt-5.4": "883.6k in, 17.7k out, 788.5k cached"}
    assert result.tokens_line == "↑ 883.6k • ↓ 17.7k • 788.5k (cached)"
    assert result.session_id == "sess-123"
    mock_impl.assert_awaited_once()


def test_implement_failure_returns_one(capsys):
    mock_impl = AsyncMock(return_value=TaskResult(status="failed", premium_requests=2))
    with ExitStack() as stack:
        _enter_patches(stack, _implement_patches(mock_impl), IMPLEMENT_ENV)
        from worker import main

        exit_code = asyncio.run(main())

    assert exit_code == 1
    result = TaskResult.model_validate_json(capsys.readouterr().out.strip())
    assert result.status == "failed"


def test_implement_exception_posts_error_comment(capsys):
    from services.copilot import TaskError

    mock_impl = AsyncMock(side_effect=TaskError("CLI crashed", premium_requests=3))
    safe = AsyncMock()
    patches = [
        patch("worker.implement_issue", mock_impl),
        patch("worker.get_issue", new_callable=AsyncMock, return_value=GitHubIssue(title="test")),
        patch("worker.safe_comment", safe),
        patch("worker.comment_on_issue", new_callable=AsyncMock, return_value=100),
        patch(
            "worker.find_issue_comment_by_body_prefix",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("worker.update_comment", new_callable=AsyncMock),
    ]
    with ExitStack() as stack:
        _enter_patches(stack, patches, IMPLEMENT_ENV)
        from worker import main

        exit_code = asyncio.run(main())

    assert exit_code == 1
    result = TaskResult.model_validate_json(capsys.readouterr().out.strip())
    assert result.status == "failed"
    assert result.premium_requests == 3

    error_calls = [c for c in safe.call_args_list if "failed" in str(c).lower()]
    assert len(error_calls) > 0


def test_review_success_returns_zero(capsys):
    mock_review = AsyncMock(return_value=TaskResult(status="complete", premium_requests=2))
    env = {**REVIEW_ENV, "MODEL": "gpt-5.4", "REASONING_EFFORT": "high"}
    with ExitStack() as stack:
        _enter_patches(stack, _review_patches(mock_review), env)
        from worker import main

        exit_code = asyncio.run(main())

    assert exit_code == 0
    result = TaskResult.model_validate_json(capsys.readouterr().out.strip())
    assert result.status == "complete"
    mock_review.assert_awaited_once()


def test_review_posts_progress_comment():
    mock_review = AsyncMock(return_value=TaskResult(status="complete", premium_requests=1))
    comment = AsyncMock(return_value=100)
    patches = [
        patch("worker.review_pr", mock_review),
        patch("worker.safe_comment", new_callable=AsyncMock),
        patch("worker.comment_on_issue", comment),
        patch(
            "worker.find_issue_comment_by_body_prefix",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("worker.update_comment", new_callable=AsyncMock),
    ]
    with ExitStack() as stack:
        _enter_patches(stack, patches, REVIEW_ENV)
        from worker import main

        asyncio.run(main())

    progress_calls = [c for c in comment.call_args_list if "Review in progress" in str(c)]
    assert len(progress_calls) > 0


def test_implement_content_rejection_returns_one(capsys):
    """Content trust rejection should not post error comments."""
    mock_impl = AsyncMock(side_effect=ValueError("not trusted"))
    with ExitStack() as stack:
        _enter_patches(stack, _implement_patches(mock_impl), IMPLEMENT_ENV)
        from worker import main

        exit_code = asyncio.run(main())

    assert exit_code == 1
    result = TaskResult.model_validate_json(capsys.readouterr().out.strip())
    assert result.status == "rejected"


def test_unknown_task_type_returns_one():
    env = {"TASK_TYPE": "unknown", "REPO": "user/repo", "NUMBER": "1", "GH_TOKEN": "ghs_test"}
    with patch.dict(os.environ, env):
        from worker import main

        exit_code = asyncio.run(main())
        assert exit_code == 1


def test_missing_env_var_returns_one():
    with patch.dict(os.environ, {}, clear=True):
        from worker import main

        exit_code = asyncio.run(main())
        assert exit_code == 1


def test_worker_supports_legacy_worker_env_aliases(capsys):
    mock_review = AsyncMock(return_value=TaskResult(status="complete"))
    legacy_env = {
        "WORKER_TASK": "review",
        "WORKER_REPO": "user/repo",
        "WORKER_PR_NUMBER": "42",
        "GH_TOKEN": "ghs_test",
    }
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, legacy_env, clear=True))
        for p in _review_patches(mock_review):
            stack.enter_context(p)
        from worker import main

        exit_code = asyncio.run(main())

    assert exit_code == 0
    result = TaskResult.model_validate_json(capsys.readouterr().out.strip())
    assert result.status == "complete"
