"""Tests for the implement lifecycle orchestrator."""

import asyncio
import itertools
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from copilot import CLIResult
from git import RebaseConflictError
from implement import _cli_stage_stats, _format_stage_stats


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


class TestImplementIssue:
    """Tests for implement_issue() — the implement+review+fix lifecycle."""

    def _standard_mocks(
        self,
        *,
        review_events: list[str] | None = None,
        review_event: str = "APPROVE",
        session_id: str | None = "sess-123",
        pr_sequence: list[dict] | None = None,
        ci_results: list[dict] | None = None,
        merge_result: dict | list[dict] | None = None,
        review_threads: str = "",
    ):
        """Create the standard set of mocks for implement_issue tests.

        Args:
            review_events: Per-round review events. Overrides review_event.
            review_event: Single review event (used when review_events is None).
            session_id: Session ID the CLI returns (None to test no-session path).
            review_threads: Inline findings text for REQUEST_CHANGES reviews.
        """
        if review_events is None:
            review_events = [review_event]

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

        # Build per-round review results
        default_threads = "- **file.py:10**\n  bug here"
        review_results = []
        for evt in review_events:
            threads = review_threads or (default_threads if evt == "REQUEST_CHANGES" else "")
            review_results.append(
                {
                    "original_event": evt,
                    "premium_requests": 1,
                    "review_threads": threads,
                    "session_id": "review-sess-1",
                }
            )

        # run_copilot: first call is implement, then one fix per REQUEST_CHANGES round
        copilot_side_effect = [mock_cli_result]
        for evt in review_events:
            if evt == "REQUEST_CHANGES":
                copilot_side_effect.append(mock_fix_result)

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
                side_effect=copilot_side_effect,
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
        """Happy path: implement → PR → review approves → merge."""
        result = self._run_with_mocks(self._standard_mocks(review_event="APPROVE"))
        assert result["status"] == "complete"
        assert result["merged"] is True
        assert result["merge_commit_sha"] == "merge123"
        assert result["pr_number"] == 99
        assert result["review_rounds"] == 1

    def test_pr_body_uses_auto_close_keyword(self):
        """Generated PR body uses a GitHub auto-closing keyword."""
        mocks = self._standard_mocks(review_event="APPROVE")

        async def run():
            with ExitStack() as stack:
                entered = [stack.enter_context(m) for m in mocks]
                create_pr_mock = entered[5]
                from implement import implement_issue

                await implement_issue(repo="user/repo", issue_number=42)

                assert "Closes #42." in create_pr_mock.await_args.kwargs["body"]

        asyncio.run(run())

    def test_review_requests_changes_then_fixes_and_merges(self):
        """Round 1 requests changes → fix → round 2 approves → merge."""
        mocks = self._standard_mocks(review_events=["REQUEST_CHANGES", "APPROVE"])

        async def run():
            with ExitStack() as stack:
                entered = [stack.enter_context(m) for m in mocks]
                copilot_mock = entered[3]
                commit_mock = entered[4]
                review_mock = entered[6]
                from implement import implement_issue

                result = await implement_issue(repo="user/repo", issue_number=42)

                # Review called twice (round 1 + round 2)
                assert review_mock.await_count == 2
                # Copilot called twice: implement + fix
                assert copilot_mock.await_count == 2
                # Commit called twice: initial + fix
                assert commit_mock.await_count == 2
                assert result["status"] == "complete"
                assert result["merged"] is True
                assert result["review_rounds"] == 2

        asyncio.run(run())

    def test_two_rounds_of_changes_fixes_both_and_merges(self):
        """Both rounds request changes → fix twice → merge."""
        mocks = self._standard_mocks(review_events=["REQUEST_CHANGES", "REQUEST_CHANGES"])

        async def run():
            with ExitStack() as stack:
                entered = [stack.enter_context(m) for m in mocks]
                copilot_mock = entered[3]
                commit_mock = entered[4]
                review_mock = entered[6]
                from implement import implement_issue

                result = await implement_issue(repo="user/repo", issue_number=42)

                # Review called twice (max rounds)
                assert review_mock.await_count == 2
                # Copilot called 3 times: implement + fix1 + fix2
                assert copilot_mock.await_count == 3
                # Commit called 3 times: initial + fix1 + fix2
                assert commit_mock.await_count == 3
                assert result["status"] == "complete"
                assert result["merged"] is True
                assert result["review_rounds"] == 2

        asyncio.run(run())

    def test_review_passes_session_id_across_rounds(self):
        """The review session_id from round 1 is passed to round 2."""
        mocks = self._standard_mocks(review_events=["REQUEST_CHANGES", "APPROVE"])

        async def run():
            with ExitStack() as stack:
                entered = [stack.enter_context(m) for m in mocks]
                review_mock = entered[6]
                from implement import implement_issue

                await implement_issue(repo="user/repo", issue_number=42)

                # First review: no prior session
                first_call = review_mock.await_args_list[0]
                assert first_call.kwargs.get("session_id") is None
                # Second review: session from round 1
                second_call = review_mock.await_args_list[1]
                assert second_call.kwargs.get("session_id") == "review-sess-1"

        asyncio.run(run())

    def test_review_failure_still_merges(self):
        """Review errors don't block — lifecycle proceeds to merge."""
        from copilot import TaskError

        mocks = self._standard_mocks(review_event="APPROVE")
        mocks[6] = patch(
            "implement.review_pr",
            new_callable=AsyncMock,
            side_effect=TaskError("review exploded", premium_requests=1),
        )

        result = self._run_with_mocks(mocks)
        assert result["status"] == "complete"
        assert result["merged"] is True

    def test_no_session_bails_out_with_partial(self):
        """Without session ID, fix can't run — returns partial for manual attention."""
        result = self._run_with_mocks(
            self._standard_mocks(review_event="REQUEST_CHANGES", session_id=None)
        )
        assert result["status"] == "partial"
        assert result["merged"] is False
        assert "no session ID" in result["error"]

    def test_no_threads_bails_out_with_partial(self):
        """REQUEST_CHANGES with no inline findings — returns partial for manual attention."""
        mocks = self._standard_mocks(review_event="REQUEST_CHANGES", review_threads="")
        # Override review to return empty threads
        mocks[6] = patch(
            "implement.review_pr",
            new_callable=AsyncMock,
            return_value={
                "original_event": "REQUEST_CHANGES",
                "premium_requests": 1,
                "review_threads": "",
                "session_id": "review-sess-1",
            },
        )
        result = self._run_with_mocks(mocks)
        assert result["status"] == "partial"
        assert result["merged"] is False
        assert "no inline findings" in result["error"]

    def test_fix_no_changes_bails_out_with_partial(self):
        """Fix produces no file changes — returns partial for manual attention."""
        mocks = self._standard_mocks(review_event="REQUEST_CHANGES")
        mocks[4] = patch(
            "implement.commit_and_push",
            new_callable=AsyncMock,
            side_effect=[
                "abc123",  # initial commit succeeds
                RuntimeError("No changes to commit"),  # fix commit fails
            ],
        )
        result = self._run_with_mocks(mocks)
        assert result["status"] == "partial"
        assert result["merged"] is False
        assert "no changes" in result["error"]

    def test_waits_for_pending_ci_before_merging(self):
        """Pending CI is polled until checks pass and the PR can be merged."""
        mocks = self._standard_mocks(
            review_event="APPROVE",
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
            review_event="APPROVE",
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

    def test_rebase_success_updates_sha_and_merges(self):
        """Dirty PRs are rebased once, then merged with the new head SHA."""
        rebased_sha = "rebased456"
        mocks = self._standard_mocks(
            review_event="APPROVE",
            pr_sequence=[
                {
                    "state": "open",
                    "draft": False,
                    "merged": False,
                    "mergeable": False,
                    "mergeable_state": "dirty",
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
                    "head": {"ref": "agent/issue-42", "sha": rebased_sha},
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
        )
        mocks[10] = patch("implement.comment_on_issue", new_callable=AsyncMock, return_value=2002)
        mocks.append(
            patch("implement.rebase_onto_main", new_callable=AsyncMock, return_value=rebased_sha)
        )
        mocks.append(patch("implement.update_comment", new_callable=AsyncMock))

        async def run():
            with ExitStack() as stack:
                entered = [stack.enter_context(m) for m in mocks]
                ci_mock = entered[8]
                merge_mock = entered[9]
                sleep_mock = entered[12]
                rebase_mock = entered[13]
                update_mock = entered[14]
                from implement import implement_issue

                result = await implement_issue(repo="user/repo", issue_number=42)

                rebase_mock.assert_awaited_once_with(
                    Path("/tmp/wt"),
                    repo_url="https://github.com/user/repo.git",
                    token="token",
                    repo="user/repo",
                    branch="agent/issue-42",
                )
                assert ci_mock.await_args_list[1].args == ("user/repo", rebased_sha)
                merge_mock.assert_awaited_once_with("user/repo", 99, sha=rebased_sha)
                assert any(
                    call.args == ("user/repo", 2002, "🔄 Rebasing onto main...")
                    for call in update_mock.await_args_list
                )
                sleep_mock.assert_awaited_once()
                assert result["status"] == "complete"
                assert result["commit_sha"] == rebased_sha
                assert result["merged"] is True

        asyncio.run(run())

    def test_rebase_conflict_returns_partial(self):
        """Rebase conflicts abort the lifecycle with a manual-resolution partial."""
        mocks = self._standard_mocks(
            review_event="APPROVE",
            pr_sequence=[
                {
                    "state": "open",
                    "draft": False,
                    "merged": False,
                    "mergeable": False,
                    "mergeable_state": "dirty",
                    "user": {"login": "colins-homelab-bot[bot]"},
                    "head": {"ref": "agent/issue-42", "sha": "abc123"},
                }
            ],
            ci_results=[
                {
                    "state": "success",
                    "description": "All required CI checks passed",
                }
            ],
        )
        mocks[10] = patch("implement.comment_on_issue", new_callable=AsyncMock, return_value=2002)
        mocks.append(
            patch(
                "implement.rebase_onto_main",
                new_callable=AsyncMock,
                side_effect=RebaseConflictError(
                    "CONFLICT (content): Merge conflict in implement.py"
                ),
            )
        )
        mocks.append(patch("implement.update_comment", new_callable=AsyncMock))

        async def run():
            with ExitStack() as stack:
                entered = [stack.enter_context(m) for m in mocks]
                merge_mock = entered[9]
                rebase_mock = entered[13]
                from implement import implement_issue

                result = await implement_issue(repo="user/repo", issue_number=42)

                rebase_mock.assert_awaited_once()
                merge_mock.assert_not_awaited()
                assert result["status"] == "partial"
                assert result["merged"] is False
                assert result["commit_sha"] == "abc123"
                assert "Rebase onto main failed with conflicts" in result["error"]
                assert "needs manual resolution" in result["error"]
                assert "CONFLICT" in result["error"]

        asyncio.run(run())

    def test_rebase_only_attempted_once(self):
        """A second dirty state waits instead of trying another rebase."""
        rebased_sha = "rebased456"
        mocks = self._standard_mocks(
            review_event="APPROVE",
            pr_sequence=[
                {
                    "state": "open",
                    "draft": False,
                    "merged": False,
                    "mergeable": False,
                    "mergeable_state": "dirty",
                    "user": {"login": "colins-homelab-bot[bot]"},
                    "head": {"ref": "agent/issue-42", "sha": "abc123"},
                },
                {
                    "state": "open",
                    "draft": False,
                    "merged": False,
                    "mergeable": False,
                    "mergeable_state": "dirty",
                    "user": {"login": "colins-homelab-bot[bot]"},
                    "head": {"ref": "agent/issue-42", "sha": rebased_sha},
                },
                {
                    "state": "open",
                    "draft": False,
                    "merged": False,
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "user": {"login": "colins-homelab-bot[bot]"},
                    "head": {"ref": "agent/issue-42", "sha": rebased_sha},
                },
            ],
            ci_results=[
                {
                    "state": "success",
                    "description": "All required CI checks passed",
                },
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
        mocks[10] = patch("implement.comment_on_issue", new_callable=AsyncMock, return_value=2002)
        mocks.append(
            patch("implement.rebase_onto_main", new_callable=AsyncMock, return_value=rebased_sha)
        )
        mocks.append(patch("implement.update_comment", new_callable=AsyncMock))

        async def run():
            with ExitStack() as stack:
                entered = [stack.enter_context(m) for m in mocks]
                merge_mock = entered[9]
                sleep_mock = entered[12]
                rebase_mock = entered[13]
                from implement import implement_issue

                result = await implement_issue(repo="user/repo", issue_number=42)

                rebase_mock.assert_awaited_once()
                assert sleep_mock.await_count == 2
                merge_mock.assert_awaited_once_with("user/repo", 99, sha=rebased_sha)
                assert result["status"] == "complete"
                assert result["commit_sha"] == rebased_sha
                assert result["merged"] is True

        asyncio.run(run())

    def test_merges_when_state_is_non_clean_but_github_accepts(self):
        """A mergeable non-clean state should not block a merge GitHub allows."""
        result = self._run_with_mocks(
            self._standard_mocks(
                review_event="APPROVE",
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
            review_event="APPROVE",
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
                review_event="APPROVE",
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
            review_event="APPROVE",
            merge_result={
                "merged": False,
                "message": "Pull Request is not mergeable",
                "status_code": 405,
            },
        )
        pr_state = {
            "state": "open",
            "draft": False,
            "merged": False,
            "mergeable": True,
            "mergeable_state": "clean",
            "user": {"login": "colins-homelab-bot[bot]"},
            "head": {"ref": "agent/issue-42", "sha": "abc123"},
        }
        ci_state = {
            "state": "success",
            "description": "All required CI checks passed",
        }
        mocks[7] = patch(
            "implement.get_pr",
            new_callable=AsyncMock,
            side_effect=itertools.repeat(pr_state),
        )
        mocks[8] = patch(
            "implement.get_commit_ci_status",
            new_callable=AsyncMock,
            side_effect=itertools.repeat(ci_state),
        )
        # time.monotonic() call order:
        # 1. implement_issue: start = 0
        # 2. _merge_when_eligible: deadline = 0 + 900 = 900
        # 3. while check (iter 1): 0 < 900 → enter
        #    (loop body: get_pr, CI, merge rejected, sleep, continue)
        # 4. while check (iter 2): 1000 >= 900 → exit loop
        # 5. _lifecycle_result: elapsed = 1000 - 0 = 1000
        mocks.append(
            patch(
                "implement._monotonic",
                side_effect=itertools.chain([0, 0, 0, 1000, 1000], itertools.repeat(1000)),
            )
        )
        result = self._run_with_mocks(mocks)
        assert result["status"] == "partial"
        assert result["merged"] is False
        assert "Timed out" in result["error"]

    def test_non_bot_pr_returns_partial(self):
        """Auto-merge refuses PRs that are no longer bot-authored."""
        result = self._run_with_mocks(
            self._standard_mocks(
                review_event="APPROVE",
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
        mocks = self._standard_mocks(review_event="APPROVE")
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

    def test_accumulates_premium_requests(self):
        """Premium requests from implement + reviews + fixes are accumulated."""
        result = self._run_with_mocks(
            self._standard_mocks(review_events=["REQUEST_CHANGES", "APPROVE"])
        )
        # 1 (implement) + 1 (review1) + 1 (fix) + 1 (review2) = 4
        assert result["premium_requests"] == 4

    def test_accumulates_premium_requests_two_fix_rounds(self):
        """Premium requests from implement + 2 reviews + 2 fixes are accumulated."""
        result = self._run_with_mocks(
            self._standard_mocks(review_events=["REQUEST_CHANGES", "REQUEST_CHANGES"])
        )
        # 1 (implement) + 1 (review1) + 1 (fix1) + 1 (review2) + 1 (fix2) = 5
        assert result["premium_requests"] == 5
