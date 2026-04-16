"""Tests for the implement lifecycle orchestrator."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from conftest import MOCK_CLI_RESULT, MOCK_ISSUE

from implement import implement_issue
from models import GitHubIssue, GitHubPullRequest
from services.copilot import CLIResult, TaskError
from stats import cli_stage_stats, format_stage_stats

_MOD = "implement.orchestrator"

# Sentinel for _base_mocks default parameter detection
_UNSET = object()

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

    def _base_mocks(self, *, pr_data=None, cli_result=None, final_pr_data=_UNSET):
        """Return standard patches for implement_issue tests.

        When ``pr_data`` is an unmerged PR the review-fix loop runs.
        ``final_pr_data`` controls what ``find_pr_by_branch`` returns on
        the second call (after merge).  Defaults to a merged copy of
        ``pr_data`` so the happy-path completes without extra setup.
        """
        already_merged = pr_data and (pr_data.merged_at or pr_data.merged)

        if pr_data and not already_merged:
            if final_pr_data is _UNSET:
                final_pr_data = GitHubPullRequest(
                    number=pr_data.number,
                    html_url=pr_data.html_url,
                    merged_at="2025-01-01T00:00:00Z",
                    merged=True,
                )
            find_pr_patch = patch(
                "implement.orchestrator.find_pr_by_branch",
                new_callable=AsyncMock,
                side_effect=[pr_data, final_pr_data],
            )
        else:
            find_pr_patch = patch(
                "implement.orchestrator.find_pr_by_branch",
                new_callable=AsyncMock,
                return_value=pr_data,
            )

        return [
            patch(f"{_MOD}._utcnow", return_value=_TEST_START),
            patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
            patch(f"{_MOD}.get_issue", new_callable=AsyncMock, return_value=MOCK_ISSUE),
            patch(
                "implement.orchestrator.create_branch_worktree",
                new_callable=AsyncMock,
                return_value=Path("/tmp/wt"),
            ),
            patch(
                "implement.orchestrator.run_copilot",
                new_callable=AsyncMock,
                return_value=cli_result or MOCK_CLI_RESULT,
            ),
            find_pr_patch,
            patch(f"{_MOD}.get_unresolved_review_threads", new_callable=AsyncMock, return_value=[]),
            patch(f"{_MOD}.merge_pr", new_callable=AsyncMock, return_value=True),
            patch(f"{_MOD}.mark_pr_ready", new_callable=AsyncMock),
            patch(f"{_MOD}.close_issue", new_callable=AsyncMock),
            patch(f"{_MOD}.safe_comment", new_callable=AsyncMock),
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
        pr_data = GitHubPullRequest(
            number=99,
            html_url="https://github.com/user/repo/pull/99",
            merged_at="2025-01-01T00:00:00Z",
            merged=True,
        )
        mocks = self._base_mocks(pr_data=pr_data)
        result = self._run(mocks)
        assert result.status == "complete"
        assert result.merged is True
        assert result.repo == "user/repo"
        assert result.pr_number == 99
        assert result.premium_requests == 5
        assert result.api_time_seconds == 60
        assert result.models == {"gpt-5.4": "883.6k in, 17.7k out, 788.5k cached"}
        assert result.tokens_line == "↑ 883.6k • ↓ 17.7k • 788.5k (cached)"
        assert result.session_id == "sess-123"

    def test_partial_when_merge_fails(self):
        """Loop approves but merge fails → status partial."""
        pr_data = GitHubPullRequest(
            number=99,
            html_url="https://github.com/user/repo/pull/99",
            merged_at=None,
            merged=False,
        )
        # merge_pr fails → CLI fallback → find_pr_by_branch in fallback + final
        # Calls: 1) after implement, 2) in _try_merge fallback, 3) final refetch
        mocks = self._base_mocks(pr_data=pr_data, final_pr_data=pr_data)
        for i, m in enumerate(mocks):
            if getattr(m, "attribute", None) == "merge_pr":
                mocks[i] = patch(f"{_MOD}.merge_pr", new_callable=AsyncMock, return_value=False)
            # Need 3 calls: after implement, in _try_merge fallback, final refetch
            if getattr(m, "attribute", None) == "find_pr_by_branch":
                mocks[i] = patch(
                    f"{_MOD}.find_pr_by_branch",
                    new_callable=AsyncMock,
                    side_effect=[pr_data, pr_data, pr_data],
                )
        result = self._run(mocks)
        assert result.status == "partial"
        assert result.merged is False

    def test_complete_when_auto_merge_enabled(self):
        """Loop approves, merge fails, but auto-merge set → status complete."""
        pr_data = GitHubPullRequest.model_validate(
            {
                "number": 99,
                "html_url": "https://github.com/user/repo/pull/99",
                "merged_at": None,
                "merged": False,
                "auto_merge": {"enabled_by": {"login": "bot"}, "merge_method": "squash"},
            }
        )
        # merge_pr fails, CLI fallback fails, but auto_merge → complete
        # find_pr_by_branch: 3 calls (implement, _try_merge fallback, final)
        mocks = self._base_mocks(pr_data=pr_data, final_pr_data=pr_data)
        for i, m in enumerate(mocks):
            if getattr(m, "attribute", None) == "merge_pr":
                mocks[i] = patch(f"{_MOD}.merge_pr", new_callable=AsyncMock, return_value=False)
            if getattr(m, "attribute", None) == "find_pr_by_branch":
                mocks[i] = patch(
                    f"{_MOD}.find_pr_by_branch",
                    new_callable=AsyncMock,
                    side_effect=[pr_data, pr_data, pr_data],
                )
        result = self._run(mocks)
        assert result.status == "complete"
        assert result.merged is False
        assert result.auto_merge is True
        assert result.error is None

    def test_failed_when_no_pr_created(self):
        """CLI doesn't create a PR → status failed."""
        mocks = self._base_mocks(pr_data=None)
        result = self._run(mocks)
        assert result.status == "failed"
        assert result.repo == "user/repo"
        assert result.pr_number is None
        assert result.error is not None
        assert "did not create a PR" in result.error

    def test_rejects_untrusted_issue_author(self):
        """Issues authored by unknown users raise ValueError."""
        untrusted_issue = GitHubIssue.model_validate(
            {
                "title": "Evil",
                "body": "Malicious",
                "user": {"login": "attacker"},
            }
        )

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
        pr_data = GitHubPullRequest(number=1, html_url="u", merged_at=None, merged=False)
        merged_pr = GitHubPullRequest(
            number=1, html_url="u", merged_at="2025-01-01T00:00:00Z", merged=True
        )

        async def run():
            with (
                patch(f"{_MOD}._utcnow", return_value=_TEST_START),
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
                patch(f"{_MOD}.get_issue", new_callable=AsyncMock, return_value=MOCK_ISSUE),
                patch(
                    "implement.orchestrator.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.orchestrator.run_copilot",
                    new_callable=AsyncMock,
                    return_value=MOCK_CLI_RESULT,
                ) as mock_cli,
                patch(
                    f"{_MOD}.find_pr_by_branch",
                    new_callable=AsyncMock,
                    side_effect=[pr_data, merged_pr],
                ),
                patch(
                    f"{_MOD}.get_unresolved_review_threads",
                    new_callable=AsyncMock,
                    return_value=[],
                ),
                patch(f"{_MOD}.merge_pr", new_callable=AsyncMock, return_value=True),
                patch(f"{_MOD}.mark_pr_ready", new_callable=AsyncMock),
                patch(f"{_MOD}.close_issue", new_callable=AsyncMock),
                patch(f"{_MOD}.safe_comment", new_callable=AsyncMock),
                patch(f"{_MOD}.cleanup_branch_worktree", new_callable=AsyncMock),
            ):
                await implement_issue(repo="user/repo", issue_number=42)
                return mock_cli

        mock_cli = asyncio.run(run())
        assert mock_cli.await_args.kwargs["github_token"] == "token"

    def test_closes_issue_on_merge(self):
        """Issue is closed when the PR is merged."""
        pr_data = GitHubPullRequest(
            number=99,
            html_url="https://github.com/user/repo/pull/99",
            merged_at="2025-01-01T00:00:00Z",
            merged=True,
        )

        async def run():
            with (
                patch(f"{_MOD}._utcnow", return_value=_TEST_START),
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
                patch(f"{_MOD}.get_issue", new_callable=AsyncMock, return_value=MOCK_ISSUE),
                patch(
                    "implement.orchestrator.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.orchestrator.run_copilot",
                    new_callable=AsyncMock,
                    return_value=MOCK_CLI_RESULT,
                ),
                patch(f"{_MOD}.find_pr_by_branch", new_callable=AsyncMock, return_value=pr_data),
                patch(f"{_MOD}.close_issue", new_callable=AsyncMock) as mock_close,
                patch(f"{_MOD}.safe_comment", new_callable=AsyncMock),
                patch(f"{_MOD}.cleanup_branch_worktree", new_callable=AsyncMock),
            ):
                await implement_issue(repo="user/repo", issue_number=42)
                return mock_close

        mock_close = asyncio.run(run())
        mock_close.assert_awaited_once_with("user/repo", 42)

    def test_does_not_close_issue_when_not_merged(self):
        """Issue stays open when merge fails after review approval."""
        pr_data = GitHubPullRequest(
            number=99,
            html_url="https://github.com/user/repo/pull/99",
            merged_at=None,
            merged=False,
        )

        async def run():
            with (
                patch(f"{_MOD}._utcnow", return_value=_TEST_START),
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
                patch(f"{_MOD}.get_issue", new_callable=AsyncMock, return_value=MOCK_ISSUE),
                patch(
                    "implement.orchestrator.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.orchestrator.run_copilot",
                    new_callable=AsyncMock,
                    return_value=MOCK_CLI_RESULT,
                ),
                # find_pr_by_branch: 3 calls (implement, _try_merge fallback, final)
                patch(
                    f"{_MOD}.find_pr_by_branch",
                    new_callable=AsyncMock,
                    side_effect=[pr_data, pr_data, pr_data],
                ),
                patch(
                    f"{_MOD}.get_unresolved_review_threads",
                    new_callable=AsyncMock,
                    return_value=[],
                ),
                patch(f"{_MOD}.merge_pr", new_callable=AsyncMock, return_value=False),
                patch(f"{_MOD}.mark_pr_ready", new_callable=AsyncMock),
                patch(f"{_MOD}.close_issue", new_callable=AsyncMock) as mock_close,
                patch(f"{_MOD}.safe_comment", new_callable=AsyncMock),
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
                    return_value=MOCK_ISSUE,
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
        pr_data = GitHubPullRequest(
            number=99,
            html_url="https://github.com/user/repo/pull/99",
            merged_at="2025-01-01T00:00:00Z",
            merged=True,
        )
        cli_result = CLIResult(output="done", total_premium_requests=7)
        mocks = self._base_mocks(pr_data=pr_data, cli_result=cli_result)
        result = self._run(mocks)
        assert result.premium_requests == 7

    def test_stats_comment_includes_cli_models_and_api_time(self):
        """Final PR stats comment includes rich CLI details."""
        pr_data = GitHubPullRequest(
            number=99,
            html_url="https://github.com/user/repo/pull/99",
            merged_at="2025-01-01T00:00:00Z",
            merged=True,
        )

        async def run():
            with (
                patch(f"{_MOD}._utcnow", return_value=_TEST_START),
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
                patch(f"{_MOD}.get_issue", new_callable=AsyncMock, return_value=MOCK_ISSUE),
                patch(
                    "implement.orchestrator.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    "implement.orchestrator.run_copilot",
                    new_callable=AsyncMock,
                    return_value=MOCK_CLI_RESULT,
                ),
                patch(f"{_MOD}.find_pr_by_branch", new_callable=AsyncMock, return_value=pr_data),
                patch(f"{_MOD}.close_issue", new_callable=AsyncMock),
                patch(f"{_MOD}.safe_comment", new_callable=AsyncMock) as mock_comment,
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
        pr_data = GitHubPullRequest(
            number=99,
            html_url="https://github.com/user/repo/pull/99",
            merged_at="2025-01-01T00:00:00Z",
            merged=True,
        )
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
                patch(f"{_MOD}.get_issue", new_callable=AsyncMock, return_value=MOCK_ISSUE),
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
                patch(f"{_MOD}.safe_comment", new_callable=AsyncMock) as mock_comment,
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
        stale_pr = GitHubPullRequest(
            number=50,
            html_url="https://github.com/user/repo/pull/50",
            state="closed",
            merged_at="2024-06-01T00:00:00Z",
            merged=True,
        )
        mocks = self._base_mocks(pr_data=stale_pr)
        result = self._run(mocks)
        assert result.status == "failed"
        assert result.pr_number is None

    def test_ignores_closed_unmerged_pr(self):
        """A closed-without-merge PR is ignored (stale branch)."""
        closed_pr = GitHubPullRequest(
            number=50,
            html_url="https://github.com/user/repo/pull/50",
            state="closed",
            merged_at=None,
            merged=False,
        )
        mocks = self._base_mocks(pr_data=closed_pr)
        result = self._run(mocks)
        assert result.status == "failed"
        assert result.pr_number is None

    def test_always_cleans_up_worktree(self):
        """Worktree is cleaned up even on failure."""
        cli_error = TaskError("boom", premium_requests=0)

        async def run():
            with (
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
                patch(
                    "implement.orchestrator.get_issue",
                    new_callable=AsyncMock,
                    return_value=MOCK_ISSUE,
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


class TestReviewFixLoop:
    """Tests for the review-fix loop in implement_issue()."""

    # PR that is open (not merged, not draft-related — just open)
    _OPEN_PR = GitHubPullRequest(
        number=99,
        html_url="https://github.com/user/repo/pull/99",
        merged_at=None,
        merged=False,
    )

    # PR after merge
    _MERGED_PR = GitHubPullRequest(
        number=99,
        html_url="https://github.com/user/repo/pull/99",
        merged_at="2025-01-01T00:00:00Z",
        merged=True,
    )

    def _run_with_loop(
        self,
        *,
        unresolved_threads_sequence: list[list] | None = None,
        review_side_effects: list | None = None,
        fix_side_effects: list | None = None,
        merge_result: bool = True,
        pr_after_merge: GitHubPullRequest | None = None,
    ):
        """Run implement_issue with mocked review-fix loop behavior.

        unresolved_threads_sequence: list of return values for successive
            get_unresolved_review_threads calls (after review, after fix, ...).
        """
        if unresolved_threads_sequence is None:
            unresolved_threads_sequence = [[]]  # clean review

        threads_iter = iter(unresolved_threads_sequence)
        review_cli = CLIResult(output="review done", total_premium_requests=1, session_id="rev-1")
        fix_cli = CLIResult(output="fix done", total_premium_requests=1)

        # run_copilot calls: implement, then review/fix alternating
        copilot_calls: list[CLIResult | TaskError] = [MOCK_CLI_RESULT]  # implement
        if review_side_effects:
            copilot_calls.extend(review_side_effects)
        else:
            # Default: review succeeds, fix succeeds (enough for 2 rounds)
            for _ in range(4):
                copilot_calls.append(review_cli)
                if fix_side_effects:
                    copilot_calls.extend(fix_side_effects)
                else:
                    copilot_calls.append(fix_cli)

        call_index = 0

        async def mock_copilot(*args, **kwargs):
            nonlocal call_index
            idx = call_index
            call_index += 1
            if idx < len(copilot_calls):
                val = copilot_calls[idx]
                if isinstance(val, Exception):
                    raise val
                return val
            return fix_cli

        # find_pr_by_branch: first call returns open PR, subsequent may return merged
        find_pr_calls = 0

        async def mock_find_pr(*args, **kwargs):
            nonlocal find_pr_calls
            find_pr_calls += 1
            if find_pr_calls == 1:
                return self._OPEN_PR
            return pr_after_merge or (self._MERGED_PR if merge_result else self._OPEN_PR)

        async def mock_threads(*args, **kwargs):
            try:
                return next(threads_iter)
            except StopIteration:
                return []

        async def run():
            with (
                patch(f"{_MOD}._utcnow", return_value=_TEST_START),
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
                patch(f"{_MOD}.get_issue", new_callable=AsyncMock, return_value=MOCK_ISSUE),
                patch(
                    f"{_MOD}.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(f"{_MOD}.run_copilot", side_effect=mock_copilot),
                patch(f"{_MOD}.find_pr_by_branch", side_effect=mock_find_pr),
                patch(f"{_MOD}.get_unresolved_review_threads", side_effect=mock_threads),
                patch(f"{_MOD}.merge_pr", new_callable=AsyncMock, return_value=merge_result),
                patch(f"{_MOD}.mark_pr_ready", new_callable=AsyncMock),
                patch(f"{_MOD}.close_issue", new_callable=AsyncMock),
                patch(f"{_MOD}.safe_comment", new_callable=AsyncMock),
                patch(f"{_MOD}.cleanup_branch_worktree", new_callable=AsyncMock),
            ):
                return await implement_issue(repo="user/repo", issue_number=42)

        return asyncio.run(run())

    def test_approved_on_first_review_then_merge(self):
        """Review finds no issues → merge immediately."""
        result = self._run_with_loop(
            unresolved_threads_sequence=[[]],  # no threads after review
        )
        assert result.status == "complete"
        assert result.merged is True

    def test_fix_resolves_issues_then_merge(self):
        """Review finds issues → fix resolves them → merge."""
        result = self._run_with_loop(
            unresolved_threads_sequence=[
                [{"id": "t1"}],  # after review: 1 thread
                [],  # after fix: resolved
            ],
        )
        assert result.status == "complete"
        assert result.merged is True

    def test_max_rounds_exceeded_leaves_pr_open(self):
        """Two rounds of review-fix, still unresolved → PR left open."""
        result = self._run_with_loop(
            unresolved_threads_sequence=[
                [{"id": "t1"}],  # round 1 review
                [{"id": "t1"}],  # round 1 fix — still there
                [{"id": "t1"}],  # round 2 review
                [{"id": "t1"}],  # round 2 fix — still there
            ],
            merge_result=False,
            pr_after_merge=self._OPEN_PR,
        )
        assert result.status == "partial"
        assert result.merged is False

    def test_no_pr_skips_loop(self):
        """Implement creates no PR → failed, no loop."""

        async def run():
            with (
                patch(f"{_MOD}._utcnow", return_value=_TEST_START),
                patch(f"{_MOD}.get_token", new_callable=AsyncMock, return_value="token"),
                patch(f"{_MOD}.get_issue", new_callable=AsyncMock, return_value=MOCK_ISSUE),
                patch(
                    f"{_MOD}.create_branch_worktree",
                    new_callable=AsyncMock,
                    return_value=Path("/tmp/wt"),
                ),
                patch(
                    f"{_MOD}.run_copilot",
                    new_callable=AsyncMock,
                    return_value=MOCK_CLI_RESULT,
                ),
                patch(
                    f"{_MOD}.find_pr_by_branch",
                    new_callable=AsyncMock,
                    return_value=None,
                ),
                patch(f"{_MOD}.safe_comment", new_callable=AsyncMock),
                patch(f"{_MOD}.cleanup_branch_worktree", new_callable=AsyncMock),
            ):
                return await implement_issue(repo="user/repo", issue_number=42)

        result = asyncio.run(run())
        assert result.status == "failed"
        assert result.pr_number is None

    def test_already_merged_skips_loop(self):
        """CLI merged during implement → skip review loop."""
        result = self._run_with_loop(
            unresolved_threads_sequence=[],  # should never be called
            pr_after_merge=self._MERGED_PR,
        )
        assert result.status == "complete"
        assert result.merged is True

    def test_review_failure_breaks_loop(self):
        """Review CLI throws TaskError → break loop, leave PR open."""
        result = self._run_with_loop(
            review_side_effects=[TaskError("review crashed", premium_requests=2)],
            merge_result=False,
            pr_after_merge=self._OPEN_PR,
        )
        assert result.status == "partial"
        assert result.merged is False
        assert result.premium_requests >= 2

    def test_fix_failure_breaks_loop(self):
        """Fix CLI throws TaskError → break loop, leave PR open."""
        review_cli = CLIResult(output="review", total_premium_requests=1, session_id="r1")
        result = self._run_with_loop(
            unresolved_threads_sequence=[[{"id": "t1"}]],  # review finds issues
            review_side_effects=[review_cli, TaskError("fix crashed", premium_requests=3)],
            merge_result=False,
            pr_after_merge=self._OPEN_PR,
        )
        assert result.status == "partial"
        assert result.merged is False

    def test_merge_failure_returns_partial(self):
        """REST merge fails, CLI fallback also fails → partial."""
        merge_fallback_fail = TaskError("merge failed", premium_requests=1)
        result = self._run_with_loop(
            unresolved_threads_sequence=[[]],  # approved
            merge_result=False,
            review_side_effects=[
                CLIResult(output="review", total_premium_requests=1, session_id="r1"),
                merge_fallback_fail,  # CLI merge fallback
            ],
            pr_after_merge=self._OPEN_PR,
        )
        assert result.status == "partial"

    def test_premium_requests_accumulate_across_loop(self):
        """Premium requests from all CLI calls are summed."""
        result = self._run_with_loop(
            unresolved_threads_sequence=[
                [{"id": "t1"}],  # after review
                [],  # after fix
            ],
        )
        # MOCK_CLI_RESULT (implement) = 5, review = 1, fix = 1
        assert result.premium_requests >= 7
