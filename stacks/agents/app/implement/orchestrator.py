"""Issue implementation orchestrator — dispatches to Copilot CLI with full repo access."""

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from models import GitHubIssue, GitHubPullRequest, TaskResult
from services.copilot import CLIResult, TaskError, run_copilot
from services.git import cleanup_branch_worktree, create_branch_worktree
from services.github import (
    close_issue,
    find_pr_by_branch,
    get_issue,
    get_token,
    get_unresolved_review_threads,
    lock_pr,
    mark_pr_ready,
    merge_pr,
    safe_comment,
)
from stats import STATUS_EMOJI, cli_stage_stats
from trust import is_trusted_content_author

logger = logging.getLogger(__name__)

MAX_REVIEW_FIX_ROUNDS = 2


def _monotonic() -> float:
    return time.monotonic()


def _utcnow() -> datetime:
    return datetime.now(UTC)


IMPLEMENT_PROMPT_TEMPLATE = """\
Implement the following GitHub issue in {repo}.

## Issue #{issue_number}: {title}

{body}

Create a draft PR when your changes are ready. Do NOT merge the PR — \
the orchestrator handles merge after automated review.

Use the bot-implement skill for guidelines on how to make changes.
"""


REVIEW_PROMPT_TEMPLATE = """\
Review PR #{pr_number} in {repo}.

This PR implements issue #{issue_number}: {issue_title}

### Issue description

{issue_body}

Read the PR diff with `gh pr diff {pr_number}`. Post your review directly via `gh`.

Use the bot-review skill for guidelines on how to review and post your findings.
"""


FIX_PROMPT_TEMPLATE = """\
Review comments have been posted on PR #{pr_number} in {repo}.

Here are the unresolved review threads:

{threads_summary}

For each thread:
- If the comment identifies a real issue: fix the code
- If the comment is a false positive: reply briefly explaining why, then resolve \
the thread using `gh api graphql` with the resolveReviewThread mutation

Push your changes when done. Do NOT merge the PR.
"""


MERGE_PROMPT_TEMPLATE = """\
PR #{pr_number} in {repo} has been approved but could not be merged automatically \
(likely due to merge conflicts or the branch being out of date).

1. Update branch: `git fetch origin main && git merge origin/main`
2. Resolve any conflicts
3. Push: `git push origin {branch}`
4. Wait for CI: `gh pr checks {pr_number} --watch`
5. Merge: `gh pr merge {pr_number} --squash`
"""


def _is_stale_pr(pr_data: GitHubPullRequest, run_start: datetime) -> bool:
    """Check if a PR predates the current run (reused branch name).

    A PR is stale if it was closed without merging, or merged before
    this run started (leftover from a previous implementation attempt).
    """
    state = pr_data.state
    merged_at_str = pr_data.merged_at

    # Closed without merging — definitely stale
    if state == "closed" and not merged_at_str:
        return True

    # Merged before this run started — leftover from a previous run
    if merged_at_str:
        merged_at = datetime.fromisoformat(merged_at_str.replace("Z", "+00:00"))
        if merged_at < run_start:
            return True

    return False


def _build_result(
    pr_data: GitHubPullRequest | None,
    elapsed: float,
    premium_requests: int,
    cli_result: CLIResult,
    repo: str,
) -> TaskResult:
    """Build a TaskResult from PR state after CLI completes."""
    if not pr_data:
        return TaskResult(
            status="failed",
            error="CLI did not create a PR",
            repo=repo,
            elapsed_seconds=elapsed,
            premium_requests=premium_requests,
            api_time_seconds=cli_result.api_time_seconds,
            models=cli_result.models,
            tokens_line=cli_result.tokens_line,
            session_id=cli_result.session_id,
        )

    merged = pr_data.merged_at is not None or pr_data.merged

    if merged:
        return TaskResult(
            status="complete",
            merged=True,
            pr_number=pr_data.number,
            pr_url=pr_data.html_url,
            repo=repo,
            elapsed_seconds=elapsed,
            premium_requests=premium_requests,
            api_time_seconds=cli_result.api_time_seconds,
            models=cli_result.models,
            tokens_line=cli_result.tokens_line,
            session_id=cli_result.session_id,
        )

    if pr_data.auto_merge is not None:
        return TaskResult(
            status="complete",
            merged=False,
            auto_merge=True,
            pr_number=pr_data.number,
            pr_url=pr_data.html_url,
            repo=repo,
            elapsed_seconds=elapsed,
            premium_requests=premium_requests,
            api_time_seconds=cli_result.api_time_seconds,
            models=cli_result.models,
            tokens_line=cli_result.tokens_line,
            session_id=cli_result.session_id,
        )

    return TaskResult(
        status="partial",
        merged=False,
        error="CLI created PR but did not merge — needs manual attention",
        pr_number=pr_data.number,
        pr_url=pr_data.html_url,
        repo=repo,
        elapsed_seconds=elapsed,
        premium_requests=premium_requests,
        api_time_seconds=cli_result.api_time_seconds,
        models=cli_result.models,
        tokens_line=cli_result.tokens_line,
        session_id=cli_result.session_id,
    )


async def _run_review_fix_loop(
    *,
    worktree_path: Path,
    repo: str,
    pr_number: int,
    issue_data: GitHubIssue,
    issue_number: int,
    implement_session_id: str | None,
    model: str,
    reasoning_effort: str,
    token: str,
) -> tuple[bool, int]:
    """Run the review → fix loop. Returns (approved, premium_requests_used)."""
    total_premium = 0
    review_session_id: str | None = None
    approved = False

    for round_num in range(1, MAX_REVIEW_FIX_ROUNDS + 1):
        # ── Review step ──
        logger.info("Review round %d for %s#%d", round_num, repo, pr_number)
        review_prompt = REVIEW_PROMPT_TEMPLATE.format(
            pr_number=pr_number,
            repo=repo,
            issue_number=issue_number,
            issue_title=issue_data.title,
            issue_body=issue_data.body or "(no description)",
        )
        try:
            review_result = await run_copilot(
                worktree_path,
                review_prompt,
                model=model,
                effort=reasoning_effort,
                session_id=review_session_id,
                github_token=token,
            )
            total_premium += review_result.total_premium_requests
            review_session_id = review_result.session_id
        except TaskError as exc:
            total_premium += exc.premium_requests
            logger.warning("Review round %d failed: %s", round_num, exc)
            await safe_comment(repo, pr_number, f"⚠️ Review round {round_num} failed — {exc}")
            break

        # Check threads after review
        try:
            unresolved = await get_unresolved_review_threads(repo, pr_number)
        except Exception:
            logger.warning(
                "Failed to fetch review threads after review round %d", round_num, exc_info=True
            )
            break
        if not unresolved:
            logger.info("Review round %d: approved (no unresolved threads)", round_num)
            approved = True
            break

        logger.info(
            "Review round %d: %d unresolved thread(s), starting fix",
            round_num,
            len(unresolved),
        )

        # ── Fix step ──
        threads_summary = "\n".join(f"- Thread {t.id}: {t.body[:200]}" for t in unresolved)
        fix_prompt = FIX_PROMPT_TEMPLATE.format(
            pr_number=pr_number, repo=repo, threads_summary=threads_summary
        )
        try:
            fix_result = await run_copilot(
                worktree_path,
                fix_prompt,
                model=model,
                effort=reasoning_effort,
                session_id=implement_session_id,
                github_token=token,
            )
            total_premium += fix_result.total_premium_requests
        except TaskError as exc:
            total_premium += exc.premium_requests
            logger.warning("Fix round %d failed: %s", round_num, exc)
            await safe_comment(repo, pr_number, f"⚠️ Fix round {round_num} failed — {exc}")
            break

        # Check threads after fix
        try:
            unresolved = await get_unresolved_review_threads(repo, pr_number)
        except Exception:
            logger.warning(
                "Failed to fetch review threads after fix round %d", round_num, exc_info=True
            )
            break
        if not unresolved:
            logger.info("Fix round %d resolved all threads", round_num)
            approved = True
            break

        logger.info(
            "Fix round %d: %d thread(s) still unresolved",
            round_num,
            len(unresolved),
        )

    return approved, total_premium


async def _try_merge(
    *,
    worktree_path: Path,
    repo: str,
    pr_number: int,
    branch_name: str,
    implement_session_id: str | None,
    model: str,
    reasoning_effort: str,
    token: str,
) -> tuple[bool, int]:
    """Attempt to merge a PR. Returns (merged, premium_requests_used)."""
    # Mark PR ready (it's a draft)
    try:
        await mark_pr_ready(repo, pr_number)
    except Exception:
        logger.warning("Failed to mark PR #%d ready", pr_number, exc_info=True)

    # Fast path: REST API merge
    try:
        merged = await merge_pr(repo, pr_number)
    except Exception:
        logger.warning("REST merge API call failed for #%d", pr_number, exc_info=True)
        merged = False
    if merged:
        logger.info("PR #%d merged via REST API", pr_number)
        return True, 0

    # Fallback: CLI rebase + merge
    logger.info("REST merge failed for #%d, trying CLI fallback", pr_number)
    merge_prompt = MERGE_PROMPT_TEMPLATE.format(
        pr_number=pr_number,
        repo=repo,
        branch=branch_name,
    )
    try:
        merge_result = await run_copilot(
            worktree_path,
            merge_prompt,
            model=model,
            effort=reasoning_effort,
            session_id=implement_session_id,
            github_token=token,
        )
        premium = merge_result.total_premium_requests

        # Re-check PR state
        pr_data = await find_pr_by_branch(repo, branch_name)
        if pr_data and (pr_data.merged_at is not None or pr_data.merged):
            logger.info("PR #%d merged via CLI fallback", pr_number)
            return True, premium
        logger.warning("CLI fallback did not merge PR #%d", pr_number)
        return False, premium
    except TaskError as exc:
        logger.warning("CLI merge fallback failed: %s", exc)
        return False, exc.premium_requests


async def implement_issue(
    *,
    repo: str,
    issue_number: int,
    model: str = "gpt-5.4",
    reasoning_effort: str = "high",
    issue: GitHubIssue | None = None,
) -> TaskResult:
    """Implement a GitHub issue with review-fix loop.

    Lifecycle: implement -> (review -> fix) x N -> merge.
    Each step is a separate CLI call sharing the same worktree.
    """
    logger.info("Implementing issue %s#%d", repo, issue_number)
    start = _monotonic()
    start_wall = _utcnow()
    repo_url = f"https://github.com/{repo}.git"
    branch_name = f"agent/issue-{issue_number}"

    token = await get_token()
    issue_data = issue
    if issue_data is None:
        issue_data = await get_issue(repo, issue_number)

    if not is_trusted_content_author(issue_data):
        raise ValueError(
            f"Issue #{issue_number} author is not trusted — "
            "refusing to inject untrusted content into CLI prompt"
        )

    total_premium_requests = 0
    cli_result: CLIResult | None = None
    result: TaskResult | None = None

    try:
        worktree_path = await create_branch_worktree(branch_name, repo_url)

        # ── Step 1: Implement ──
        prompt = IMPLEMENT_PROMPT_TEMPLATE.format(
            repo=repo,
            issue_number=issue_number,
            title=issue_data.title,
            body=issue_data.body or "(no description)",
        )

        cli_result = await run_copilot(
            worktree_path,
            prompt,
            model=model,
            effort=reasoning_effort,
            github_token=token,
        )
        total_premium_requests += cli_result.total_premium_requests
        implement_session_id = cli_result.session_id

        pr_data = await find_pr_by_branch(repo, branch_name)

        if pr_data and _is_stale_pr(pr_data, start_wall):
            logger.warning(
                "Ignoring stale PR #%d for %s (state=%s, merged_at=%s)",
                pr_data.number,
                branch_name,
                pr_data.state,
                pr_data.merged_at,
            )
            pr_data = None

        # No PR created → fail early
        if not pr_data:
            elapsed = _monotonic() - start
            result = _build_result(None, elapsed, total_premium_requests, cli_result, repo)
            return result

        # Lock the PR to prevent external comment injection
        try:
            await lock_pr(repo, pr_data.number)
        except Exception:
            logger.warning("Failed to lock PR #%d", pr_data.number, exc_info=True)

        # CLI already merged the PR → skip review loop
        if pr_data.merged_at is not None or pr_data.merged:
            logger.info("CLI already merged PR #%d, skipping review loop", pr_data.number)
            elapsed = _monotonic() - start
            result = _build_result(pr_data, elapsed, total_premium_requests, cli_result, repo)
            if result.merged:
                try:
                    await close_issue(repo, issue_number)
                except Exception:
                    logger.warning(
                        "Failed to close issue %s#%d after merged PR",
                        repo,
                        issue_number,
                        exc_info=True,
                    )
            return result

        # ── Step 2: Review-fix loop ──
        approved, loop_premium = await _run_review_fix_loop(
            worktree_path=worktree_path,
            repo=repo,
            pr_number=pr_data.number,
            issue_data=issue_data,
            issue_number=issue_number,
            implement_session_id=implement_session_id,
            model=model,
            reasoning_effort=reasoning_effort,
            token=token,
        )
        total_premium_requests += loop_premium

        # ── Step 3: Merge or leave open ──
        if approved:
            _merged, merge_premium = await _try_merge(
                worktree_path=worktree_path,
                repo=repo,
                pr_number=pr_data.number,
                branch_name=branch_name,
                implement_session_id=implement_session_id,
                model=model,
                reasoning_effort=reasoning_effort,
                token=token,
            )
            total_premium_requests += merge_premium

            # Re-fetch PR state for final result
            pr_data = await find_pr_by_branch(repo, branch_name)
        else:
            await safe_comment(
                repo,
                pr_data.number,
                f"⚠️ Review-fix loop exhausted ({MAX_REVIEW_FIX_ROUNDS} rounds) "
                "with unresolved threads — needs human review.",
            )

        elapsed = _monotonic() - start
        result = _build_result(pr_data, elapsed, total_premium_requests, cli_result, repo)

        if result.merged:
            try:
                await close_issue(repo, issue_number)
            except Exception:
                logger.warning(
                    "Failed to close issue %s#%d after merged PR",
                    repo,
                    issue_number,
                    exc_info=True,
                )

        return result

    except TaskError:
        raise
    except Exception as exc:
        raise TaskError(str(exc), premium_requests=total_premium_requests) from exc

    finally:
        if result and result.pr_number is not None:
            stats = cli_stage_stats(cli_result, effort=reasoning_effort) if cli_result else ""
            emoji = STATUS_EMOJI.get(result.status, "❓")
            await safe_comment(
                repo,
                result.pr_number,
                f"{emoji} **Implementation {result.status}**\n{stats}",
            )
        await cleanup_branch_worktree(branch_name)
