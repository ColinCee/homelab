"""Tests for review orchestrator — linked issues and dispatch."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from review import review_pr
from review.orchestrator import (
    _fetch_linked_issues_section,
    _parse_linked_issues,
)
from services.copilot import CLIResult

_MOD = "review.orchestrator"


class TestParseLinkedIssues:
    def test_fixes_hashtag(self):
        assert _parse_linked_issues("Fixes #42") == [42]

    def test_closes_hashtag(self):
        assert _parse_linked_issues("Closes #7") == [7]

    def test_resolves_hashtag(self):
        assert _parse_linked_issues("Resolves #99") == [99]

    def test_case_insensitive(self):
        assert _parse_linked_issues("FIXES #1") == [1]

    def test_multiple_issues(self):
        assert _parse_linked_issues("Fixes #1, closes #2") == [1, 2]

    def test_deduplicates(self):
        assert _parse_linked_issues("Fixes #1. Also fixes #1") == [1]

    def test_no_matches(self):
        assert _parse_linked_issues("No issues here") == []

    def test_ignores_bare_hashtag(self):
        assert _parse_linked_issues("See #42 for details") == []

    def test_fix_singular(self):
        assert _parse_linked_issues("Fix #5") == [5]

    def test_closed_past_tense(self):
        assert _parse_linked_issues("Closed #3") == [3]

    def test_resolved_past_tense(self):
        assert _parse_linked_issues("Resolved #8") == [8]


class TestFetchLinkedIssuesSection:
    def test_returns_empty_when_no_links(self):
        async def run():
            return await _fetch_linked_issues_section("user/repo", "No links here")

        assert asyncio.run(run()) == ""

    @patch(f"{_MOD}.get_issue", new_callable=AsyncMock)
    def test_fetches_and_formats_linked_issue(self, mock_get_issue: AsyncMock):
        mock_get_issue.return_value = {
            "title": "Bug",
            "body": "Fix it",
            "user": {"login": "ColinCee"},
        }

        async def run():
            return await _fetch_linked_issues_section("user/repo", "Fixes #10")

        result = asyncio.run(run())
        assert "### #10: Bug" in result
        assert "Fix it" in result

    @patch(f"{_MOD}.get_issue", new_callable=AsyncMock)
    def test_skips_issues_that_fail_to_fetch(self, mock_get_issue: AsyncMock):
        mock_get_issue.side_effect = RuntimeError("not found")

        async def run():
            return await _fetch_linked_issues_section("user/repo", "Fixes #10")

        assert asyncio.run(run()) == ""

    @patch(f"{_MOD}.get_issue", new_callable=AsyncMock)
    def test_handles_issue_with_no_body(self, mock_get_issue: AsyncMock):
        mock_get_issue.return_value = {
            "title": "No body",
            "body": None,
            "user": {"login": "ColinCee"},
        }

        async def run():
            return await _fetch_linked_issues_section("user/repo", "Fixes #5")

        result = asyncio.run(run())
        assert "_No body._" in result

    @patch(f"{_MOD}.get_issue", new_callable=AsyncMock)
    def test_skips_untrusted_issue_authors(self, mock_get_issue: AsyncMock):
        mock_get_issue.return_value = {
            "title": "Evil",
            "body": "Malicious prompt",
            "user": {"login": "attacker"},
        }

        async def run():
            return await _fetch_linked_issues_section("user/repo", "Fixes #5")

        assert asyncio.run(run()) == ""

    @patch(f"{_MOD}.get_issue", new_callable=AsyncMock)
    def test_rejects_issue_with_missing_user(self, mock_get_issue: AsyncMock):
        mock_get_issue.return_value = {
            "title": "No user field",
            "body": "Suspicious",
        }

        async def run():
            return await _fetch_linked_issues_section("user/repo", "Fixes #5")

        assert asyncio.run(run()) == ""


class TestReviewPr:
    def test_dispatches_cli_and_returns_stats(self):
        """review_pr sets up worktree, runs CLI with GH_TOKEN, and returns stats."""
        cli_result = CLIResult(
            output="",
            total_premium_requests=5,
            session_id="sess-1",
            session_time_seconds=120,
            api_time_seconds=60,
            models={"gpt-5.4": "10 requests"},
            tokens_line="100k input, 5k output",
        )

        pr_data = {
            "title": "Fix bug",
            "body": "Fixes #1",
            "base": {"ref": "main"},
            "head": {"ref": "agent/issue-1", "repo": {"full_name": "user/repo"}},
        }

        async def run():
            with (
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
                patch(f"{_MOD}.get_pr", new_callable=AsyncMock, return_value=pr_data),
                patch(
                    f"{_MOD}.get_issue",
                    new_callable=AsyncMock,
                    return_value={
                        "title": "Bug",
                        "body": "Fix",
                        "user": {"login": "ColinCee"},
                    },
                ),
                patch(f"{_MOD}.create_worktree", new_callable=AsyncMock, return_value="/tmp/wt"),
                patch(f"{_MOD}.run_copilot", new_callable=AsyncMock, return_value=cli_result),
                patch(f"{_MOD}.cleanup_worktree", new_callable=AsyncMock),
            ):
                return await review_pr(repo="user/repo", pr_number=1)

        result = asyncio.run(run())
        assert result["status"] == "complete"
        assert result["premium_requests"] == 5
        assert result["session_id"] == "sess-1"

    def test_passes_github_token_to_cli(self):
        """CLI receives GH_TOKEN for direct review posting."""
        cli_result = CLIResult(output="", total_premium_requests=1)
        pr_data = {
            "title": "T",
            "body": "",
            "base": {"ref": "main"},
            "head": {"ref": "b", "repo": {"full_name": "user/repo"}},
        }

        async def run():
            with (
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="my-token"),
                patch(f"{_MOD}.get_pr", new_callable=AsyncMock, return_value=pr_data),
                patch(f"{_MOD}.create_worktree", new_callable=AsyncMock, return_value="/tmp/wt"),
                patch(
                    f"{_MOD}.run_copilot", new_callable=AsyncMock, return_value=cli_result
                ) as mock_cli,
                patch(f"{_MOD}.cleanup_worktree", new_callable=AsyncMock),
            ):
                await review_pr(repo="user/repo", pr_number=1)
                return mock_cli

        mock_cli = asyncio.run(run())
        assert mock_cli.await_args.kwargs["github_token"] == "my-token"

    def test_rejects_fork_prs(self):
        """PRs from forks are rejected to prevent giving GH_TOKEN to untrusted code."""
        pr_data = {
            "title": "Malicious PR",
            "body": "Fixes #1",
            "base": {"ref": "main"},
            "head": {"ref": "evil-branch", "repo": {"full_name": "attacker/repo"}},
        }

        async def run():
            with (
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
                patch(f"{_MOD}.get_pr", new_callable=AsyncMock, return_value=pr_data),
                patch(f"{_MOD}.cleanup_worktree", new_callable=AsyncMock),
            ):
                return await review_pr(repo="user/repo", pr_number=1)

        with pytest.raises(ValueError, match="fork"):
            asyncio.run(run())

    def test_rejects_deleted_fork_repo(self):
        """PRs where the fork repo was deleted (repo: null) are also rejected."""
        pr_data = {
            "title": "Deleted fork PR",
            "body": "",
            "base": {"ref": "main"},
            "head": {"ref": "branch", "repo": None},
        }

        async def run():
            with (
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
                patch(f"{_MOD}.get_pr", new_callable=AsyncMock, return_value=pr_data),
                patch(f"{_MOD}.cleanup_worktree", new_callable=AsyncMock),
            ):
                return await review_pr(repo="user/repo", pr_number=1)

        with pytest.raises(ValueError, match="fork"):
            asyncio.run(run())
