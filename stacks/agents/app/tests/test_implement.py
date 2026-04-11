"""Tests for the implement lifecycle orchestrator."""

import asyncio
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from copilot import CLIResult


class TestImplementIssue:
    """Tests for implement_issue() — the unified implement+review+fix lifecycle."""

    def _standard_mocks(self, *, review_events: list[str], session_id: str | None = "sess-123"):
        """Create the standard set of mocks for implement_issue tests.

        Args:
            review_events: List of original_event values review_pr will return.
            session_id: Session ID the CLI returns (None to test no-session path).
        """
        mock_cli_result = CLIResult(output="done", total_premium_requests=1, session_id=session_id)
        mock_fix_result = CLIResult(output="fixed", total_premium_requests=1, session_id=session_id)
        mock_issue = {
            "title": "Add foo feature",
            "body": "We need foo.",
            "author_association": "OWNER",
        }
        mock_pr = {"number": 99, "html_url": "https://github.com/user/repo/pull/99"}

        review_results = [
            {"original_event": event, "premium_requests": 1} for event in review_events
        ]

        return [
            patch("implement.get_token", new_callable=AsyncMock, return_value="token"),
            patch("implement.get_issue", new_callable=AsyncMock, return_value=mock_issue),
            patch(
                "implement.create_branch_worktree",
                new_callable=AsyncMock,
                return_value=Path("/tmp/wt"),
            ),
            patch(
                "implement.run_copilot",
                new_callable=AsyncMock,
                side_effect=[mock_cli_result] + [mock_fix_result] * len(review_events),
            ),
            patch("implement.commit_and_push", new_callable=AsyncMock, return_value="abc123"),
            patch("implement.create_pull_request", new_callable=AsyncMock, return_value=mock_pr),
            patch(
                "implement.review_pr",
                new_callable=AsyncMock,
                side_effect=review_results,
            ),
            patch(
                "implement.get_unresolved_threads",
                new_callable=AsyncMock,
                return_value="- **file.py:10** — bug here",
            ),
            patch("implement.comment_on_issue", new_callable=AsyncMock),
            patch("implement.mark_pr_ready", new_callable=AsyncMock),
            patch("implement.cleanup_branch_worktree", new_callable=AsyncMock),
        ]

    def _run_with_mocks(self, mocks):
        async def run():
            with ExitStack() as stack:
                for m in mocks:
                    stack.enter_context(m)
                from implement import implement_issue

                return await implement_issue(repo="user/repo", issue_number=42)

        return asyncio.run(run())

    def test_creates_pr_and_review_approves(self):
        """Happy path: implement → PR → review approves on first pass."""
        create_pr = AsyncMock(return_value={"number": 99, "html_url": "https://url"})

        mocks = self._standard_mocks(review_events=["APPROVE"])
        mocks[5] = patch("implement.create_pull_request", create_pr)
        result = self._run_with_mocks(mocks)
        assert result["status"] == "complete"
        assert result["pr_number"] == 99
        assert result["review_rounds"] == 1
        # PR must be created as draft to avoid racing code-review.yaml
        create_pr.assert_called_once()
        _, kwargs = create_pr.call_args
        assert kwargs.get("draft") is True

    def test_review_fix_loop_converges(self):
        """Review requests changes, fix succeeds, second review approves."""
        result = self._run_with_mocks(
            self._standard_mocks(review_events=["REQUEST_CHANGES", "APPROVE"])
        )
        assert result["status"] == "complete"
        assert result["review_rounds"] == 2

    def test_max_iterations_posts_comment(self):
        """Exhausting all fix iterations returns max_iterations status."""
        # 3 fixes + 1 final review = 4 review rounds, all REQUEST_CHANGES
        result = self._run_with_mocks(self._standard_mocks(review_events=["REQUEST_CHANGES"] * 4))
        assert result["status"] == "max_iterations"
        assert result["review_rounds"] == 4

    def test_no_session_id_returns_partial(self):
        """Without session ID, cannot resume — returns partial after first review."""
        result = self._run_with_mocks(
            self._standard_mocks(review_events=["REQUEST_CHANGES"], session_id=None)
        )
        assert result["status"] == "partial"
        assert "session resumption unavailable" in result["error"]

    def test_no_threads_returns_partial(self):
        """REQUEST_CHANGES with no threads returns partial, not complete."""
        mocks = self._standard_mocks(review_events=["REQUEST_CHANGES"])
        # Override get_unresolved_threads to return empty
        mocks[7] = patch(
            "implement.get_unresolved_threads",
            new_callable=AsyncMock,
            return_value="",
        )
        result = self._run_with_mocks(mocks)
        assert result["status"] == "partial"
        assert "review body" in result["error"]

    def test_accumulates_premium_requests(self):
        """Premium requests from implement + review + fix are all accumulated."""
        result = self._run_with_mocks(
            self._standard_mocks(review_events=["REQUEST_CHANGES", "APPROVE"])
        )
        # 1 (implement) + 1 (review 1) + 1 (fix) + 1 (review 2) = 4
        assert result["premium_requests"] == 4
