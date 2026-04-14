"""Tests for the implement lifecycle orchestrator."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, patch

import pytest

from implement import implement_issue
from services.copilot import CLIResult, TaskError
from stats import cli_stage_stats, format_stage_stats

_MOD = "implement.orchestrator"

# Fixed time for tests — all mock merged_at values should be after this
_TEST_START = datetime(2024, 12, 31, tzinfo=UTC)


class TestFormatStageStats:
    def test_formats_all_fields(self):
        result = format_stage_stats(
            premium_requests=2,
            elapsed_seconds=125,
            models={"gpt-5.4": "883.6k in, 17.7k out, 788.5k cached"},
        )
        assert "💰 2 premium" in result
        assert "⏱️ 2m 5s" in result
        assert "🤖 gpt-5.4: 883.6k in, 17.7k out, 788.5k cached" in result

    def test_includes_api_time(self):
        result = format_stage_stats(
            elapsed_seconds=900,
            api_time_seconds=723,
        )
        assert "⏱️ 15m 0s (API: 12m 3s)" in result

    def test_omits_api_time_when_zero(self):
        result = format_stage_stats(elapsed_seconds=60)
        assert "(API:" not in result

    def test_includes_effort(self):
        result = format_stage_stats(
            premium_requests=1,
            effort="high",
        )
        assert "🧠 high" in result

    def test_strips_est_premium_from_models(self):
        result = format_stage_stats(
            models={"gpt-5.4": "2.2m in, 28.9k out (Est. 1 Premium request)"},
        )
        assert "(Est." not in result
        assert "2.2m in, 28.9k out" in result

    def test_empty_when_no_data(self):
        assert format_stage_stats() == ""

    def test_tokens_line_when_no_models(self):
        result = format_stage_stats(
            premium_requests=1,
            tokens_line="↑ 1.1m • ↓ 18.9k • 1.0m (cached)",
        )
        assert "📊 ↑ 1.1m • ↓ 18.9k • 1.0m (cached)" in result

    def test_models_preferred_over_tokens_line(self):
        result = format_stage_stats(
            models={"gpt-5.4": "1.2m in, 12k out"},
            tokens_line="↑ 1.1m • ↓ 18.9k",
        )
        assert "🤖 gpt-5.4" in result
        assert "📊" not in result

    def testcli_stage_stats_from_result(self):
        r = CLIResult(
            output="",
            total_premium_requests=1,
            api_time_seconds=180,
            session_time_seconds=300,
            models={"gpt-5.4": "1.2m in, 12k out"},
        )
        result = cli_stage_stats(r, effort="high")
        assert "💰 1 premium" in result
        assert "⏱️ 5m 0s (API: 3m 0s)" in result
        assert "🧠 high" in result
        assert "gpt-5.4" in result

    def testcli_stage_stats_new_format(self):
        r = CLIResult(
            output="",
            total_premium_requests=1,
            session_time_seconds=376,
            tokens_line="↑ 1.1m • ↓ 18.9k • 1.0m (cached) • 11.7k (reasoning)",
        )
        result = cli_stage_stats(r, effort="high")
        assert "💰 1 premium" in result
        assert "⏱️ 6m 16s" in result
        assert "📊 ↑ 1.1m" in result


class TestImplementIssue:
    """Tests for implement_issue() — thin dispatcher to CLI."""

    MOCK_ISSUE: ClassVar[dict] = {
        "title": "Add foo feature",
        "body": "We need foo.",
        "user": {"login": "ColinCee"},
    }

    MOCK_CLI_RESULT = CLIResult(
        output="done",
        total_premium_requests=5,
        session_id="sess-123",
        session_time_seconds=120,
        api_time_seconds=60,
        models={"gpt-5.4": "883.6k in, 17.7k out, 788.5k cached"},
        tokens_line="↑ 883.6k • ↓ 17.7k • 788.5k (cached)",
    )

    def _base_mocks(self, *, pr_data=None, cli_result=None):
        """Return standard patches for implement_issue tests."""
        return [
            patch(f"{_MOD}._utcnow", return_value=_TEST_START),
            patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
            patch(f"{_MOD}.get_issue", new_callable=AsyncMock, return_value=self.MOCK_ISSUE),
            patch(
                "implement.orchestrator.create_branch_worktree",
                new_callable=AsyncMock,
                return_value=Path("/tmp/wt"),
            ),
            patch(
                "implement.orchestrator.run_copilot",
                new_callable=AsyncMock,
                return_value=cli_result or self.MOCK_CLI_RESULT,
            ),
            patch(
                "implement.orchestrator.find_pr_by_branch",
                new_callable=AsyncMock,
                return_value=pr_data,
            ),
            patch(f"{_MOD}.close_issue", new_callable=AsyncMock),
            patch(f"{_MOD}.comment_on_issue", new_callable=AsyncMock),
            patch(f"{_MOD}.cleanup_branch_worktree", new_callable=AsyncMock),
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
        assert result["api_time_seconds"] == 60
        assert result["models"] == {"gpt-5.4": "883.6k in, 17.7k out, 788.5k cached"}
        assert result["tokens_line"] == "↑ 883.6k • ↓ 17.7k • 788.5k (cached)"
        assert result["session_id"] == "sess-123"

    def test_partial_when_pr_not_merged(self):
        """CLI creates PR but doesn't merge and no auto-merge → status partial."""
        pr_data = {
            "number": 99,
            "html_url": "https://github.com/user/repo/pull/99",
            "merged_at": None,
            "merged": False,
            "auto_merge": None,
        }
        mocks = self._base_mocks(pr_data=pr_data)
        result = self._run(mocks)
        assert result["status"] == "partial"
        assert result["merged"] is False
        assert "manual attention" in result["error"]

    def test_complete_when_auto_merge_enabled(self):
        """CLI enables auto-merge → status complete, issue not closed (GitHub handles it)."""
        pr_data = {
            "number": 99,
            "html_url": "https://github.com/user/repo/pull/99",
            "merged_at": None,
            "merged": False,
            "auto_merge": {"enabled_by": {"login": "bot"}, "merge_method": "squash"},
        }
        mocks = self._base_mocks(pr_data=pr_data)
        result = self._run(mocks)
        assert result["status"] == "complete"
        assert result["merged"] is False
        assert result["auto_merge"] is True
        assert "error" not in result

    def test_failed_when_no_pr_created(self):
        """CLI doesn't create a PR → status failed."""
        mocks = self._base_mocks(pr_data=None)
        result = self._run(mocks)
        assert result["status"] == "failed"
        assert result["pr_number"] is None
        assert "did not create a PR" in result["error"]

    def test_rejects_untrusted_issue_author(self):
        """Issues authored by unknown users raise ValueError."""
        untrusted_issue = {
            "title": "Evil",
            "body": "Malicious",
            "user": {"login": "attacker"},
        }

        async def run():
            with (
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="t"),
                patch(
                    f"{_MOD}.get_issue",
                    new_callable=AsyncMock,
                    return_value=untrusted_issue,
                ),
                patch(f"{_MOD}.cleanup_branch_worktree", new_callable=AsyncMock),
            ):
                await implement_issue(repo="user/repo", issue_number=42)

        with pytest.raises(ValueError, match="not trusted"):
            asyncio.run(run())

    def test_passes_github_token_to_cli(self):
        """CLI receives GH_TOKEN for git push and API access."""
        pr_data = {"number": 1, "html_url": "u", "merged_at": None, "merged": False}

        async def run():
            with (
                patch(f"{_MOD}._utcnow", return_value=_TEST_START),
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
                patch(f"{_MOD}.get_issue", new_callable=AsyncMock, return_value=self.MOCK_ISSUE),
                patch(
                    "implement.orchestrator.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.orchestrator.run_copilot",
                    new_callable=AsyncMock,
                    return_value=self.MOCK_CLI_RESULT,
                ) as mock_cli,
                patch(f"{_MOD}.find_pr_by_branch", new_callable=AsyncMock, return_value=pr_data),
                patch(f"{_MOD}.close_issue", new_callable=AsyncMock),
                patch(f"{_MOD}.comment_on_issue", new_callable=AsyncMock),
                patch(f"{_MOD}.cleanup_branch_worktree", new_callable=AsyncMock),
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
                patch(f"{_MOD}._utcnow", return_value=_TEST_START),
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
                patch(f"{_MOD}.get_issue", new_callable=AsyncMock, return_value=self.MOCK_ISSUE),
                patch(
                    "implement.orchestrator.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.orchestrator.run_copilot",
                    new_callable=AsyncMock,
                    return_value=self.MOCK_CLI_RESULT,
                ),
                patch(f"{_MOD}.find_pr_by_branch", new_callable=AsyncMock, return_value=pr_data),
                patch(f"{_MOD}.close_issue", new_callable=AsyncMock) as mock_close,
                patch(f"{_MOD}.comment_on_issue", new_callable=AsyncMock),
                patch(f"{_MOD}.cleanup_branch_worktree", new_callable=AsyncMock),
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
                patch(f"{_MOD}._utcnow", return_value=_TEST_START),
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
                patch(f"{_MOD}.get_issue", new_callable=AsyncMock, return_value=self.MOCK_ISSUE),
                patch(
                    "implement.orchestrator.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.orchestrator.run_copilot",
                    new_callable=AsyncMock,
                    return_value=self.MOCK_CLI_RESULT,
                ),
                patch(f"{_MOD}.find_pr_by_branch", new_callable=AsyncMock, return_value=pr_data),
                patch(f"{_MOD}.close_issue", new_callable=AsyncMock) as mock_close,
                patch(f"{_MOD}.comment_on_issue", new_callable=AsyncMock),
                patch(f"{_MOD}.cleanup_branch_worktree", new_callable=AsyncMock),
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
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "implement.orchestrator.get_issue",
                    new_callable=AsyncMock,
                    return_value=self.MOCK_ISSUE,
                ),
                patch(
                    "implement.orchestrator.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.orchestrator.run_copilot",
                    new_callable=AsyncMock,
                    side_effect=cli_error,
                ),
                patch(f"{_MOD}.cleanup_branch_worktree", new_callable=AsyncMock),
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

    def test_stats_comment_includes_cli_models_and_api_time(self):
        """Final PR stats comment includes rich CLI details."""
        pr_data = {
            "number": 99,
            "html_url": "https://github.com/user/repo/pull/99",
            "merged_at": "2025-01-01T00:00:00Z",
            "merged": True,
        }

        async def run():
            with (
                patch(f"{_MOD}._utcnow", return_value=_TEST_START),
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
                patch(f"{_MOD}.get_issue", new_callable=AsyncMock, return_value=self.MOCK_ISSUE),
                patch(
                    "implement.orchestrator.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.orchestrator.run_copilot",
                    new_callable=AsyncMock,
                    return_value=self.MOCK_CLI_RESULT,
                ),
                patch(f"{_MOD}.find_pr_by_branch", new_callable=AsyncMock, return_value=pr_data),
                patch(f"{_MOD}.close_issue", new_callable=AsyncMock),
                patch(f"{_MOD}.comment_on_issue", new_callable=AsyncMock) as mock_comment,
                patch(f"{_MOD}.cleanup_branch_worktree", new_callable=AsyncMock),
            ):
                await implement_issue(repo="user/repo", issue_number=42)
                return mock_comment

        mock_comment = asyncio.run(run())
        comment_body = mock_comment.await_args.args[2]
        assert "💰 5 premium" in comment_body
        assert "⏱️" in comment_body
        assert "(API: 1m 0s)" in comment_body
        assert "🧠 high" in comment_body
        assert "🤖 gpt-5.4: 883.6k in, 17.7k out, 788.5k cached" in comment_body

    def test_stats_comment_includes_tokens_line_when_no_models(self):
        """Final PR stats comment falls back to the tokens line for new CLI output."""
        pr_data = {
            "number": 99,
            "html_url": "https://github.com/user/repo/pull/99",
            "merged_at": "2025-01-01T00:00:00Z",
            "merged": True,
        }
        cli_result = CLIResult(
            output="done",
            total_premium_requests=1,
            session_id="sess-123",
            session_time_seconds=120,
            api_time_seconds=60,
            tokens_line="↑ 1.1m • ↓ 18.9k • 1.0m (cached)",
        )

        async def run():
            with (
                patch(f"{_MOD}._utcnow", return_value=_TEST_START),
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
                patch(f"{_MOD}.get_issue", new_callable=AsyncMock, return_value=self.MOCK_ISSUE),
                patch(
                    "implement.orchestrator.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.orchestrator.run_copilot",
                    new_callable=AsyncMock,
                    return_value=cli_result,
                ),
                patch(f"{_MOD}.find_pr_by_branch", new_callable=AsyncMock, return_value=pr_data),
                patch(f"{_MOD}.close_issue", new_callable=AsyncMock),
                patch(f"{_MOD}.comment_on_issue", new_callable=AsyncMock) as mock_comment,
                patch(f"{_MOD}.cleanup_branch_worktree", new_callable=AsyncMock),
            ):
                await implement_issue(repo="user/repo", issue_number=42)
                return mock_comment

        mock_comment = asyncio.run(run())
        comment_body = mock_comment.await_args.args[2]
        assert "📊 ↑ 1.1m • ↓ 18.9k • 1.0m (cached)" in comment_body
        assert "🤖" not in comment_body

    def test_ignores_stale_merged_pr_from_previous_run(self):
        """A PR merged before this run started is ignored (reused branch name)."""
        stale_pr = {
            "number": 50,
            "html_url": "https://github.com/user/repo/pull/50",
            "state": "closed",
            "merged_at": "2024-06-01T00:00:00Z",
            "merged": True,
        }
        mocks = self._base_mocks(pr_data=stale_pr)
        result = self._run(mocks)
        assert result["status"] == "failed"
        assert result["pr_number"] is None

    def test_ignores_closed_unmerged_pr(self):
        """A closed-without-merge PR is ignored (stale branch)."""
        closed_pr = {
            "number": 50,
            "html_url": "https://github.com/user/repo/pull/50",
            "state": "closed",
            "merged_at": None,
            "merged": False,
        }
        mocks = self._base_mocks(pr_data=closed_pr)
        result = self._run(mocks)
        assert result["status"] == "failed"
        assert result["pr_number"] is None

    def test_always_cleans_up_worktree(self):
        """Worktree is cleaned up even on failure."""
        cli_error = TaskError("boom", premium_requests=0)

        async def run():
            with (
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "implement.orchestrator.get_issue",
                    new_callable=AsyncMock,
                    return_value=self.MOCK_ISSUE,
                ),
                patch(
                    "implement.orchestrator.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.orchestrator.run_copilot",
                    new_callable=AsyncMock,
                    side_effect=cli_error,
                ),
                patch(f"{_MOD}.cleanup_branch_worktree", new_callable=AsyncMock) as mock_cleanup,
            ):
                with pytest.raises(TaskError):
                    await implement_issue(repo="user/repo", issue_number=42)
                return mock_cleanup

        mock_cleanup = asyncio.run(run())
        mock_cleanup.assert_awaited_once_with("agent/issue-42")
