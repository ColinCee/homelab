"""Tests for implement and fix orchestrators."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from copilot import CLIResult


class TestImplementIssue:
    def test_creates_pr_from_issue(self):
        mock_cli_result = CLIResult(output="done", total_premium_requests=1)
        mock_issue = {"title": "Add foo feature", "body": "We need foo."}
        mock_pr = {"number": 99, "html_url": "https://github.com/user/repo/pull/99"}

        async def run():
            with (
                patch("implement.get_token", new_callable=AsyncMock, return_value="token"),
                patch("implement.get_issue", new_callable=AsyncMock, return_value=mock_issue),
                patch(
                    "implement.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.run_copilot", new_callable=AsyncMock, return_value=mock_cli_result
                ),
                patch("implement.commit_and_push", new_callable=AsyncMock, return_value="abc123"),
                patch(
                    "implement.create_pull_request", new_callable=AsyncMock, return_value=mock_pr
                ),
                patch("implement.comment_on_issue", new_callable=AsyncMock),
                patch("implement.cleanup_branch_worktree", new_callable=AsyncMock),
            ):
                from implement import implement_issue

                result = await implement_issue(repo="user/repo", issue_number=42)

            assert result["pr_number"] == 99
            assert result["commit_sha"] == "abc123"

        asyncio.run(run())


class TestFixPR:
    def test_fixes_review_feedback(self):
        mock_cli_result = CLIResult(output="fixed", total_premium_requests=1)
        mock_pr_data = {"head": {"ref": "agent/issue-42"}}

        async def run():
            with (
                patch("implement.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "implement.get_unresolved_threads",
                    new_callable=AsyncMock,
                    return_value="- **file.py:10** — bug here",
                ),
                patch("implement.get_pr", new_callable=AsyncMock, return_value=mock_pr_data),
                patch(
                    "implement.create_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.run_copilot", new_callable=AsyncMock, return_value=mock_cli_result
                ),
                patch("implement.commit_and_push", new_callable=AsyncMock, return_value="def456"),
                patch("implement.comment_on_issue", new_callable=AsyncMock),
                patch("implement.cleanup_worktree", new_callable=AsyncMock),
            ):
                from implement import fix_pr

                result = await fix_pr(repo="user/repo", pr_number=99)

            assert result["status"] == "fixed"
            assert result["commit_sha"] == "def456"

        asyncio.run(run())

    def test_returns_nothing_to_fix_when_no_threads(self):
        async def run():
            with (
                patch("implement.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "implement.get_unresolved_threads",
                    new_callable=AsyncMock,
                    return_value="",
                ),
            ):
                from implement import fix_pr

                result = await fix_pr(repo="user/repo", pr_number=99)

            assert result["status"] == "nothing_to_fix"

        asyncio.run(run())
