"""Tests for the implement lifecycle orchestrator."""

import asyncio
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from copilot import CLIResult


class TestImplementIssue:
    """Tests for implement_issue() — the unified implement+review+fix lifecycle."""

    def _standard_mocks(
        self,
        *,
        review_events: list[str],
        session_id: str | None = "sess-123",
        pr_sequence: list[dict] | None = None,
        ci_results: list[dict] | None = None,
        merge_result: dict | list[dict] | None = None,
    ):
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
        mock_live_pr = {
            "state": "open",
            "draft": False,
            "merged": False,
            "mergeable": True,
            "mergeable_state": "clean",
            "merge_commit_sha": None,
            "user": {"login": "colins-homelab-bot[bot]"},
            "head": {"ref": "agent/issue-42", "sha": "abc123"},
        }
        if pr_sequence is None:
            pr_sequence = [mock_live_pr]
        if ci_results is None:
            ci_results = [{"state": "success", "description": "All required CI checks passed"}]
        if merge_result is None:
            merge_result = {"merged": True, "sha": "merge123"}
        if isinstance(merge_result, list):
            merge_patch = patch(
                "implement.merge_pull_request",
                new_callable=AsyncMock,
                side_effect=merge_result,
            )
        else:
            merge_patch = patch(
                "implement.merge_pull_request",
                new_callable=AsyncMock,
                return_value=merge_result,
            )

        review_results = [
            {
                "original_event": event,
                "premium_requests": 1,
                "review_threads": (
                    "- **file.py:10**\n  bug here" if event == "REQUEST_CHANGES" else ""
                ),
            }
            for event in review_events
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
                "implement.get_pr",
                new_callable=AsyncMock,
                side_effect=pr_sequence,
            ),
            patch(
                "implement.get_commit_ci_status",
                new_callable=AsyncMock,
                side_effect=ci_results,
            ),
            merge_patch,
            patch("implement.comment_on_issue", new_callable=AsyncMock),
            patch("implement.cleanup_branch_worktree", new_callable=AsyncMock),
            patch("implement.asyncio.sleep", new_callable=AsyncMock),
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
        result = self._run_with_mocks(self._standard_mocks(review_events=["APPROVE"]))
        assert result["status"] == "complete"
        assert result["merged"] is True
        assert result["merge_commit_sha"] == "merge123"
        assert result["pr_number"] == 99
        assert result["review_rounds"] == 1

    def test_pr_body_uses_auto_close_keyword(self):
        """Generated PR body uses a GitHub auto-closing keyword."""
        mocks = self._standard_mocks(review_events=["APPROVE"])

        async def run():
            with ExitStack() as stack:
                entered = [stack.enter_context(m) for m in mocks]
                create_pr_mock = entered[5]
                from implement import implement_issue

                await implement_issue(repo="user/repo", issue_number=42)

                assert "Closes #42." in create_pr_mock.await_args.kwargs["body"]

        asyncio.run(run())

    def test_review_fix_loop_converges(self):
        """Review requests changes, fix succeeds, second review approves."""
        result = self._run_with_mocks(
            self._standard_mocks(review_events=["REQUEST_CHANGES", "APPROVE"])
        )
        assert result["status"] == "complete"
        assert result["merged"] is True
        assert result["review_rounds"] == 2

    def test_waits_for_pending_ci_before_merging(self):
        """Pending CI is polled until checks pass and the PR can be merged."""
        mocks = self._standard_mocks(
            review_events=["APPROVE"],
            pr_sequence=[
                {
                    "state": "open",
                    "draft": False,
                    "merged": False,
                    "mergeable": None,
                    "mergeable_state": "unknown",
                    "user": {"login": "colins-homelab-bot[bot]"},
                    "head": {"ref": "agent/issue-42", "sha": "abc123"},
                },
                {
                    "state": "open",
                    "draft": False,
                    "merged": False,
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "user": {"login": "colins-homelab-bot[bot]"},
                    "head": {"ref": "agent/issue-42", "sha": "abc123"},
                },
            ],
            ci_results=[
                {
                    "state": "pending",
                    "description": "Required CI checks still running: check",
                },
                {
                    "state": "success",
                    "description": "All required CI checks passed",
                },
            ],
        )

        async def run():
            with ExitStack() as stack:
                entered = [stack.enter_context(m) for m in mocks]
                merge_mock = entered[9]
                sleep_mock = entered[12]
                from implement import implement_issue

                result = await implement_issue(repo="user/repo", issue_number=42)

                sleep_mock.assert_awaited_once()
                merge_mock.assert_awaited_once()
                assert result["status"] == "complete"
                assert result["merged"] is True

        asyncio.run(run())

    def test_waits_when_branch_protection_blocks_merge_until_ci_finishes(self):
        """Protected branches can report blocked/unmergeable until required checks finish."""
        mocks = self._standard_mocks(
            review_events=["APPROVE"],
            pr_sequence=[
                {
                    "state": "open",
                    "draft": False,
                    "merged": False,
                    "mergeable": False,
                    "mergeable_state": "blocked",
                    "user": {"login": "colins-homelab-bot[bot]"},
                    "head": {"ref": "agent/issue-42", "sha": "abc123"},
                },
                {
                    "state": "open",
                    "draft": False,
                    "merged": False,
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "user": {"login": "colins-homelab-bot[bot]"},
                    "head": {"ref": "agent/issue-42", "sha": "abc123"},
                },
            ],
            ci_results=[
                {
                    "state": "pending",
                    "description": "Required CI checks still running: check",
                },
                {
                    "state": "success",
                    "description": "All required CI checks passed",
                },
            ],
        )

        async def run():
            with ExitStack() as stack:
                entered = [stack.enter_context(m) for m in mocks]
                merge_mock = entered[9]
                sleep_mock = entered[12]
                from implement import implement_issue

                result = await implement_issue(repo="user/repo", issue_number=42)

                sleep_mock.assert_awaited_once()
                merge_mock.assert_awaited_once()
                assert result["status"] == "complete"
                assert result["merged"] is True

        asyncio.run(run())

    def test_merges_when_state_is_non_clean_but_github_accepts(self):
        """A mergeable non-clean state should not block a merge GitHub allows."""
        result = self._run_with_mocks(
            self._standard_mocks(
                review_events=["APPROVE"],
                pr_sequence=[
                    {
                        "state": "open",
                        "draft": False,
                        "merged": False,
                        "mergeable": True,
                        "mergeable_state": "unstable",
                        "user": {"login": "colins-homelab-bot[bot]"},
                        "head": {"ref": "agent/issue-42", "sha": "abc123"},
                    }
                ],
            )
        )
        assert result["status"] == "complete"
        assert result["merged"] is True
        assert result["mergeable_state"] == "unstable"

    def test_retries_non_clean_merge_rejection_until_timeout_or_success(self):
        """Transient merge API rejections in non-clean states are retried."""
        mocks = self._standard_mocks(
            review_events=["APPROVE"],
            pr_sequence=[
                {
                    "state": "open",
                    "draft": False,
                    "merged": False,
                    "mergeable": True,
                    "mergeable_state": "unstable",
                    "user": {"login": "colins-homelab-bot[bot]"},
                    "head": {"ref": "agent/issue-42", "sha": "abc123"},
                },
                {
                    "state": "open",
                    "draft": False,
                    "merged": False,
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "user": {"login": "colins-homelab-bot[bot]"},
                    "head": {"ref": "agent/issue-42", "sha": "abc123"},
                },
            ],
            ci_results=[
                {
                    "state": "success",
                    "description": "All required CI checks passed",
                },
                {
                    "state": "success",
                    "description": "All required CI checks passed",
                },
            ],
            merge_result=[
                {
                    "merged": False,
                    "message": "Branch protection settings are still evaluating",
                    "status_code": 405,
                },
                {"merged": True, "sha": "merge123"},
            ],
        )

        async def run():
            with ExitStack() as stack:
                entered = [stack.enter_context(m) for m in mocks]
                merge_mock = entered[9]
                sleep_mock = entered[12]
                from implement import implement_issue

                result = await implement_issue(repo="user/repo", issue_number=42)

                assert merge_mock.await_count == 2
                sleep_mock.assert_awaited_once()
                assert result["status"] == "complete"
                assert result["merged"] is True

        asyncio.run(run())

    def test_ci_failure_does_not_block_merge(self):
        """GitHub's merge API is the sole authority — CI failure doesn't block."""
        result = self._run_with_mocks(
            self._standard_mocks(
                review_events=["APPROVE"],
                ci_results=[
                    {
                        "state": "failure",
                        "description": "Optional check failed",
                    }
                ],
            )
        )
        assert result["status"] == "complete"
        assert result["merged"] is True

    def test_merge_rejection_returns_partial(self):
        """Persistent merge rejection results in timeout partial."""
        mocks = self._standard_mocks(
            review_events=["APPROVE"],
            merge_result={
                "merged": False,
                "message": "Pull Request is not mergeable",
                "status_code": 405,
            },
        )
        # time.monotonic() call order:
        # 1. implement_issue: start = 0
        # 2. _merge_when_eligible: deadline = 0 + 900 = 900
        # 3. while check (iter 1): 0 < 900 → enter
        #    (loop body: get_pr, CI, merge rejected, sleep, continue)
        # 4. while check (iter 2): 1000 >= 900 → exit loop
        # 5. _lifecycle_result: elapsed = 1000 - 0 = 1000
        mocks.append(patch("implement.time.monotonic", side_effect=[0, 0, 0, 1000, 1000]))
        result = self._run_with_mocks(mocks)
        assert result["status"] == "partial"
        assert result["merged"] is False
        assert "Timed out" in result["error"]

    def test_non_bot_pr_returns_partial(self):
        """Auto-merge refuses PRs that are no longer bot-authored."""
        result = self._run_with_mocks(
            self._standard_mocks(
                review_events=["APPROVE"],
                pr_sequence=[
                    {
                        "state": "open",
                        "draft": False,
                        "merged": False,
                        "mergeable": True,
                        "mergeable_state": "clean",
                        "user": {"login": "someone-else"},
                        "head": {"ref": "agent/issue-42", "sha": "abc123"},
                    }
                ],
            )
        )
        assert result["status"] == "partial"
        assert result["merged"] is False
        assert "not bot-authored" in result["error"]

    def test_merge_polling_http_error_returns_partial(self):
        """GitHub polling errors stay explicit partial/manual-attention states."""
        mocks = self._standard_mocks(review_events=["APPROVE"])
        mocks[8] = patch(
            "implement.get_commit_ci_status",
            new_callable=AsyncMock,
            side_effect=httpx.HTTPStatusError(
                "403 Forbidden",
                request=httpx.Request("GET", "https://api.github.com/status"),
                response=httpx.Response(
                    403,
                    request=httpx.Request("GET", "https://api.github.com/status"),
                ),
            ),
        )

        result = self._run_with_mocks(mocks)
        assert result["status"] == "partial"
        assert result["pr_number"] == 99
        assert result["merged"] is False
        assert "GitHub merge polling failed" in result["error"]

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
        """REQUEST_CHANGES with no inline findings returns partial."""
        mocks = self._standard_mocks(review_events=["REQUEST_CHANGES"])
        # Override review_pr to return REQUEST_CHANGES with no inline comments
        mocks[6] = patch(
            "implement.review_pr",
            new_callable=AsyncMock,
            return_value={
                "original_event": "REQUEST_CHANGES",
                "premium_requests": 1,
                "review_threads": "",
            },
        )
        result = self._run_with_mocks(mocks)
        assert result["status"] == "partial"
        assert "no inline comments" in result["error"]

    def test_accumulates_premium_requests(self):
        """Premium requests from implement + review + fix are all accumulated."""
        result = self._run_with_mocks(
            self._standard_mocks(review_events=["REQUEST_CHANGES", "APPROVE"])
        )
        # 1 (implement) + 1 (review 1) + 1 (fix) + 1 (review 2) = 4
        assert result["premium_requests"] == 4

    def test_passes_previous_threads_to_re_review(self):
        """On second review round, previous findings are passed as context."""
        mocks = self._standard_mocks(review_events=["REQUEST_CHANGES", "APPROVE"])

        async def run():
            with ExitStack() as stack:
                entered = [stack.enter_context(m) for m in mocks]
                review_mock = entered[6]  # review_pr mock
                from implement import implement_issue

                await implement_issue(repo="user/repo", issue_number=42)

                # First call: no previous context
                assert review_mock.call_args_list[0].kwargs["previous_comments"] == ""
                # Second call: previous findings threaded through
                assert "file.py:10" in review_mock.call_args_list[1].kwargs["previous_comments"]

        asyncio.run(run())
