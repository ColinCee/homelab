"""Tests for the implement lifecycle orchestrator."""

import asyncio
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, patch

import pytest

from copilot import CLIResult, TaskError
from implement import _cli_stage_stats, _format_stage_stats, implement_issue


class TestFormatStageStats:
    def test_formats_all_fields(self):
        result = _format_stage_stats(
            premium_requests=2,
            elapsed_seconds=125,
            models={"gpt-5.4": "883.6k in, 17.7k out, 788.5k cached"},
        )
        assert "💰 2 premium" in result
        assert "⏱️ 2m 5s" in result
        assert "🤖 gpt-5.4: 883.6k in, 17.7k out, 788.5k cached" in result

    def test_includes_api_time(self):
        result = _format_stage_stats(
            elapsed_seconds=900,
            api_time_seconds=723,
        )
        assert "⏱️ 15m 0s (API: 12m 3s)" in result

    def test_omits_api_time_when_zero(self):
        result = _format_stage_stats(elapsed_seconds=60)
        assert "(API:" not in result

    def test_includes_effort(self):
        result = _format_stage_stats(
            premium_requests=1,
            effort="high",
        )
        assert "🧠 high" in result

    def test_strips_est_premium_from_models(self):
        result = _format_stage_stats(
            models={"gpt-5.4": "2.2m in, 28.9k out (Est. 1 Premium request)"},
        )
        assert "(Est." not in result
        assert "2.2m in, 28.9k out" in result

    def test_empty_when_no_data(self):
        assert _format_stage_stats() == ""

    def test_tokens_line_when_no_models(self):
        result = _format_stage_stats(
            premium_requests=1,
            tokens_line="↑ 1.1m • ↓ 18.9k • 1.0m (cached)",
        )
        assert "📊 ↑ 1.1m • ↓ 18.9k • 1.0m (cached)" in result

    def test_models_preferred_over_tokens_line(self):
        result = _format_stage_stats(
            models={"gpt-5.4": "1.2m in, 12k out"},
            tokens_line="↑ 1.1m • ↓ 18.9k",
        )
        assert "🤖 gpt-5.4" in result
        assert "📊" not in result

    def test_cli_stage_stats_from_result(self):
        r = CLIResult(
            output="",
            total_premium_requests=1,
            api_time_seconds=180,
            session_time_seconds=300,
            models={"gpt-5.4": "1.2m in, 12k out"},
        )
        result = _cli_stage_stats(r, effort="high")
        assert "💰 1 premium" in result
        assert "⏱️ 5m 0s (API: 3m 0s)" in result
        assert "🧠 high" in result
        assert "gpt-5.4" in result

    def test_cli_stage_stats_new_format(self):
        r = CLIResult(
            output="",
            total_premium_requests=1,
            session_time_seconds=376,
            tokens_line="↑ 1.1m • ↓ 18.9k • 1.0m (cached) • 11.7k (reasoning)",
        )
        result = _cli_stage_stats(r, effort="high")
        assert "💰 1 premium" in result
        assert "⏱️ 6m 16s" in result
        assert "📊 ↑ 1.1m" in result


class TestImplementIssue:
    """Tests for implement_issue() — thin dispatcher to CLI."""

    MOCK_ISSUE: ClassVar[dict] = {
        "title": "Add foo feature",
        "body": "We need foo.",
        "author_association": "OWNER",
    }

    MOCK_CLI_RESULT = CLIResult(
        output="done",
        total_premium_requests=5,
        session_id="sess-123",
        session_time_seconds=120,
        api_time_seconds=60,
    )

    def _base_mocks(self, *, pr_data=None, cli_result=None):
        """Return standard patches for implement_issue tests."""
        return [
            patch("implement.get_token", new_callable=AsyncMock, return_value="token"),
            patch("implement.get_issue", new_callable=AsyncMock, return_value=self.MOCK_ISSUE),
            patch(
                "implement.create_branch_worktree",
                new_callable=AsyncMock,
                return_value=Path("/tmp/wt"),
            ),
            patch(
                "implement.run_copilot",
                new_callable=AsyncMock,
                return_value=cli_result or self.MOCK_CLI_RESULT,
            ),
            patch(
                "implement.find_pr_by_branch",
                new_callable=AsyncMock,
                return_value=pr_data,
            ),
            patch("implement.close_issue", new_callable=AsyncMock),
            patch("implement.comment_on_issue", new_callable=AsyncMock),
            patch("implement.cleanup_branch_worktree", new_callable=AsyncMock),
        ]

    def _run(self, mocks, **kwargs):
        async def run():
            for m in mocks:
                m.start()
            try:
                return await implement_issue(repo="user/repo", issue_number=42, **kwargs)
            finally:
                for m in reversed(mocks):
                    m.stop()

        return asyncio.run(run())

    def test_complete_when_pr_merged(self):
        """CLI creates and merges PR → status complete, issue closed."""
        pr_data = {
            "number": 99,
            "html_url": "https://github.com/user/repo/pull/99",
            "merged_at": "2025-01-01T00:00:00Z",
            "merged": True,
        }
        mocks = self._base_mocks(pr_data=pr_data)
        result = self._run(mocks)
        assert result["status"] == "complete"
        assert result["merged"] is True
        assert result["pr_number"] == 99
        assert result["premium_requests"] == 5

    def test_partial_when_pr_not_merged(self):
        """CLI creates PR but doesn't merge → status partial."""
        pr_data = {
            "number": 99,
            "html_url": "https://github.com/user/repo/pull/99",
            "merged_at": None,
            "merged": False,
        }
        mocks = self._base_mocks(pr_data=pr_data)
        result = self._run(mocks)
        assert result["status"] == "partial"
        assert result["merged"] is False
        assert "manual attention" in result["error"]

    def test_failed_when_no_pr_created(self):
        """CLI doesn't create a PR → status failed."""
        mocks = self._base_mocks(pr_data=None)
        result = self._run(mocks)
        assert result["status"] == "failed"
        assert result["pr_number"] is None
        assert "did not create a PR" in result["error"]

    def test_rejects_untrusted_author(self):
        """Issues from untrusted authors raise ValueError."""
        untrusted_issue = {
            "title": "Evil",
            "body": "Malicious",
            "author_association": "NONE",
        }

        async def run():
            with (
                patch("implement.get_token", new_callable=AsyncMock, return_value="token"),
                patch("implement.get_issue", new_callable=AsyncMock, return_value=untrusted_issue),
                patch("implement.cleanup_branch_worktree", new_callable=AsyncMock),
            ):
                await implement_issue(repo="user/repo", issue_number=42)

        with pytest.raises(ValueError, match="trusted"):
            asyncio.run(run())

    def test_passes_github_token_to_cli(self):
        """CLI receives GH_TOKEN for git push and API access."""
        pr_data = {"number": 1, "html_url": "u", "merged_at": None, "merged": False}

        async def run():
            with (
                patch("implement.get_token", new_callable=AsyncMock, return_value="token"),
                patch("implement.get_issue", new_callable=AsyncMock, return_value=self.MOCK_ISSUE),
                patch(
                    "implement.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.run_copilot",
                    new_callable=AsyncMock,
                    return_value=self.MOCK_CLI_RESULT,
                ) as mock_cli,
                patch("implement.find_pr_by_branch", new_callable=AsyncMock, return_value=pr_data),
                patch("implement.close_issue", new_callable=AsyncMock),
                patch("implement.comment_on_issue", new_callable=AsyncMock),
                patch("implement.cleanup_branch_worktree", new_callable=AsyncMock),
            ):
                await implement_issue(repo="user/repo", issue_number=42)
                return mock_cli

        mock_cli = asyncio.run(run())
        assert mock_cli.await_args.kwargs["github_token"] == "token"

    def test_closes_issue_on_merge(self):
        """Issue is closed when the PR is merged."""
        pr_data = {
            "number": 99,
            "html_url": "https://github.com/user/repo/pull/99",
            "merged_at": "2025-01-01T00:00:00Z",
            "merged": True,
        }

        async def run():
            with (
                patch("implement.get_token", new_callable=AsyncMock, return_value="token"),
                patch("implement.get_issue", new_callable=AsyncMock, return_value=self.MOCK_ISSUE),
                patch(
                    "implement.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.run_copilot",
                    new_callable=AsyncMock,
                    return_value=self.MOCK_CLI_RESULT,
                ),
                patch("implement.find_pr_by_branch", new_callable=AsyncMock, return_value=pr_data),
                patch("implement.close_issue", new_callable=AsyncMock) as mock_close,
                patch("implement.comment_on_issue", new_callable=AsyncMock),
                patch("implement.cleanup_branch_worktree", new_callable=AsyncMock),
            ):
                await implement_issue(repo="user/repo", issue_number=42)
                return mock_close

        mock_close = asyncio.run(run())
        mock_close.assert_awaited_once_with("user/repo", 42)

    def test_does_not_close_issue_when_not_merged(self):
        """Issue stays open when PR is not merged."""
        pr_data = {
            "number": 99,
            "html_url": "https://github.com/user/repo/pull/99",
            "merged_at": None,
            "merged": False,
        }

        async def run():
            with (
                patch("implement.get_token", new_callable=AsyncMock, return_value="token"),
                patch("implement.get_issue", new_callable=AsyncMock, return_value=self.MOCK_ISSUE),
                patch(
                    "implement.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.run_copilot",
                    new_callable=AsyncMock,
                    return_value=self.MOCK_CLI_RESULT,
                ),
                patch("implement.find_pr_by_branch", new_callable=AsyncMock, return_value=pr_data),
                patch("implement.close_issue", new_callable=AsyncMock) as mock_close,
                patch("implement.comment_on_issue", new_callable=AsyncMock),
                patch("implement.cleanup_branch_worktree", new_callable=AsyncMock),
            ):
                await implement_issue(repo="user/repo", issue_number=42)
                return mock_close

        mock_close = asyncio.run(run())
        mock_close.assert_not_awaited()

    def test_cli_error_raises_task_error(self):
        """CLI failure propagates as TaskError with premium request count."""
        cli_error = TaskError("CLI crashed", premium_requests=3)

        async def run():
            with (
                patch("implement.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "implement.get_issue",
                    new_callable=AsyncMock,
                    return_value=self.MOCK_ISSUE,
                ),
                patch(
                    "implement.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.run_copilot",
                    new_callable=AsyncMock,
                    side_effect=cli_error,
                ),
                patch("implement.cleanup_branch_worktree", new_callable=AsyncMock),
            ):
                await implement_issue(repo="user/repo", issue_number=42)

        with pytest.raises(TaskError):
            asyncio.run(run())

    def test_accumulates_premium_requests(self):
        """Premium requests from CLI are reported in result."""
        pr_data = {
            "number": 99,
            "html_url": "https://github.com/user/repo/pull/99",
            "merged_at": "2025-01-01T00:00:00Z",
            "merged": True,
        }
        cli_result = CLIResult(output="done", total_premium_requests=7)
        mocks = self._base_mocks(pr_data=pr_data, cli_result=cli_result)
        result = self._run(mocks)
        assert result["premium_requests"] == 7

    def test_always_cleans_up_worktree(self):
        """Worktree is cleaned up even on failure."""
        cli_error = TaskError("boom", premium_requests=0)

        async def run():
            with (
                patch("implement.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "implement.get_issue",
                    new_callable=AsyncMock,
                    return_value=self.MOCK_ISSUE,
                ),
                patch(
                    "implement.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.run_copilot",
                    new_callable=AsyncMock,
                    side_effect=cli_error,
                ),
                patch("implement.cleanup_branch_worktree", new_callable=AsyncMock) as mock_cleanup,
            ):
                with pytest.raises(TaskError):
                    await implement_issue(repo="user/repo", issue_number=42)
                return mock_cleanup

        mock_cleanup = asyncio.run(run())
        mock_cleanup.assert_awaited_once_with("agent/issue-42")
