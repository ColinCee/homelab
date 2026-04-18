"""Issue implementation orchestrator — dispatches to Copilot CLI with full repo access."""

import logging
import time
from dataclasses import dataclass
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
from trust import is_trusted_content_author

logger = logging.getLogger(__name__)

MAX_REVIEW_FIX_ROUNDS = 2


def _monotonic() -> float:
    return time.monotonic()


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ── Prompts ──────────────────────────────────────────────


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


# ── Context ──────────────────────────────────────────────


@dataclass(frozen=True)
class _LoopContext:
    """Shared state for the review-fix loop and merge steps."""

    worktree_path: Path
    repo: str
    model: str
    reasoning_effort: str
    token: str
    implement_session_id: str | None


@dataclass
class _TokenAccumulator:
    """Accumulates stats across multiple CLI calls."""

    premium_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cli_calls: int = 0
    review_fix_rounds: int = 0

    def add_result(self, result: CLIResult) -> None:
        self.premium_requests += result.total_premium_requests
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        self.cached_tokens += result.cached_tokens
        self.reasoning_tokens += result.reasoning_tokens
        self.cli_calls += 1

    def add_error(self, exc: TaskError) -> None:
        self.premium_requests += exc.premium_requests
        self.input_tokens += exc.input_tokens
        self.output_tokens += exc.output_tokens
        self.cached_tokens += exc.cached_tokens
        self.reasoning_tokens += exc.reasoning_tokens
        self.cli_calls += 1


# ── Helpers ──────────────────────────────────────────────


def _is_stale_pr(pr_data: GitHubPullRequest, run_start: datetime) -> bool:
    """Check if a PR predates the current run (reused branch name).

    A PR is stale if it was closed without merging, or merged before
    this run started (leftover from a previous implementation attempt).
    """
    state = pr_data.state
    merged_at_str = pr_data.merged_at

    if state == "closed" and not merged_at_str:
        return True

    if merged_at_str:
        merged_at = datetime.fromisoformat(merged_at_str.replace("Z", "+00:00"))
        if merged_at < run_start:
            return True

    return False


def _is_merged(pr_data: GitHubPullRequest) -> bool:
    return pr_data.merged_at is not None or pr_data.merged


def _build_result(
    pr_data: GitHubPullRequest | None,
    elapsed: float,
    acc: _TokenAccumulator,
    cli_result: CLIResult | None,
    repo: str,
    model: str,
    reasoning_effort: str,
) -> TaskResult:
    """Build a TaskResult from PR state after CLI completes."""
    if not pr_data:
        return TaskResult(
            status="failed",
            error="CLI did not create a PR",
            repo=repo,
            model=model,
            reasoning_effort=reasoning_effort,
            elapsed_seconds=elapsed,
            premium_requests=acc.premium_requests,
            input_tokens=acc.input_tokens,
            output_tokens=acc.output_tokens,
            cached_tokens=acc.cached_tokens,
            reasoning_tokens=acc.reasoning_tokens,
            cli_calls=acc.cli_calls,
            review_fix_rounds=acc.review_fix_rounds,
            api_time_seconds=cli_result.api_time_seconds if cli_result else None,
            models=cli_result.models if cli_result else None,
            tokens_line=cli_result.tokens_line if cli_result else None,
            session_id=cli_result.session_id if cli_result else None,
        )

    status = "complete" if (_is_merged(pr_data) or pr_data.auto_merge is not None) else "partial"
    return TaskResult(
        status=status,
        merged=_is_merged(pr_data),
        auto_merge=pr_data.auto_merge is not None if not _is_merged(pr_data) else None,
        error=None
        if status == "complete"
        else "CLI created PR but did not merge — needs manual attention",
        pr_number=pr_data.number,
        pr_url=pr_data.html_url,
        repo=repo,
        model=model,
        reasoning_effort=reasoning_effort,
        elapsed_seconds=elapsed,
        premium_requests=acc.premium_requests,
        input_tokens=acc.input_tokens,
        output_tokens=acc.output_tokens,
        cached_tokens=acc.cached_tokens,
        reasoning_tokens=acc.reasoning_tokens,
        cli_calls=acc.cli_calls,
        review_fix_rounds=acc.review_fix_rounds,
        api_time_seconds=cli_result.api_time_seconds if cli_result else None,
        models=cli_result.models if cli_result else None,
        tokens_line=cli_result.tokens_line if cli_result else None,
        session_id=cli_result.session_id if cli_result else None,
    )


# ── Review-fix loop ─────────────────────────────────────


async def _run_review_fix_loop(
    ctx: _LoopContext,
    pr_number: int,
    issue_data: GitHubIssue,
    issue_number: int,
    acc: _TokenAccumulator,
) -> bool:
    """Run the review → fix loop. Returns whether the PR was approved."""
    review_session_id: str | None = None

    for round_num in range(1, MAX_REVIEW_FIX_ROUNDS + 1):
        acc.review_fix_rounds = round_num
        # ── Review step ──
        logger.info("Review round %d for %s#%d", round_num, ctx.repo, pr_number)
        review_prompt = REVIEW_PROMPT_TEMPLATE.format(
            pr_number=pr_number,
            repo=ctx.repo,
            issue_number=issue_number,
            issue_title=issue_data.title,
            issue_body=issue_data.body or "(no description)",
        )
        try:
            review_result = await run_copilot(
                ctx.worktree_path,
                review_prompt,
                stage="review",
                model=ctx.model,
                effort=ctx.reasoning_effort,
                session_id=review_session_id,
                github_token=ctx.token,
            )
            acc.add_result(review_result)
            review_session_id = review_result.session_id
        except TaskError as exc:
            acc.add_error(exc)
            logger.warning("Review round %d failed: %s", round_num, exc)
            await safe_comment(ctx.repo, pr_number, f"⚠️ Review round {round_num} failed — {exc}")
            break

        unresolved = await get_unresolved_review_threads(ctx.repo, pr_number)
        if unresolved is None:
            logger.warning(
                "Failed to fetch threads after review round %d, stopping loop", round_num
            )
            break
        if not unresolved:
            logger.info("Review round %d: approved (no unresolved threads)", round_num)
            return True

        logger.info(
            "Review round %d: %d unresolved thread(s), starting fix",
            round_num,
            len(unresolved),
        )

        # ── Fix step ──
        threads_summary = "\n".join(f"- Thread {t.id}: {t.body[:200]}" for t in unresolved)
        fix_prompt = FIX_PROMPT_TEMPLATE.format(
            pr_number=pr_number, repo=ctx.repo, threads_summary=threads_summary
        )
        try:
            fix_result = await run_copilot(
                ctx.worktree_path,
                fix_prompt,
                stage="fix",
                model=ctx.model,
                effort=ctx.reasoning_effort,
                session_id=ctx.implement_session_id,
                github_token=ctx.token,
            )
            acc.add_result(fix_result)
        except TaskError as exc:
            acc.add_error(exc)
            logger.warning("Fix round %d failed: %s", round_num, exc)
            await safe_comment(ctx.repo, pr_number, f"⚠️ Fix round {round_num} failed — {exc}")
            break

        unresolved = await get_unresolved_review_threads(ctx.repo, pr_number)
        if unresolved is None:
            logger.warning("Failed to fetch threads after fix round %d, stopping loop", round_num)
            break
        if not unresolved:
            logger.info("Fix round %d resolved all threads", round_num)
            return True

        logger.info(
            "Fix round %d: %d thread(s) still unresolved",
            round_num,
            len(unresolved),
        )

    return False


# ── Merge ────────────────────────────────────────────────


async def _try_merge(
    ctx: _LoopContext, pr_number: int, branch_name: str, acc: _TokenAccumulator
) -> bool:
    """Attempt to merge a PR. Returns whether the merge succeeded."""
    await mark_pr_ready(ctx.repo, pr_number)

    if await merge_pr(ctx.repo, pr_number):
        logger.info("PR #%d merged via REST API", pr_number)
        return True

    # Fallback: CLI merge
    logger.info("REST merge failed for #%d, trying CLI fallback", pr_number)
    merge_prompt = MERGE_PROMPT_TEMPLATE.format(
        pr_number=pr_number, repo=ctx.repo, branch=branch_name
    )
    try:
        merge_result = await run_copilot(
            ctx.worktree_path,
            merge_prompt,
            stage="merge",
            model=ctx.model,
            effort=ctx.reasoning_effort,
            session_id=ctx.implement_session_id,
            github_token=ctx.token,
        )
        acc.add_result(merge_result)
        pr_data = await find_pr_by_branch(ctx.repo, branch_name)
        if pr_data and _is_merged(pr_data):
            logger.info("PR #%d merged via CLI fallback", pr_number)
            return True
        logger.warning("CLI fallback did not merge PR #%d", pr_number)
        return False
    except TaskError as exc:
        acc.add_error(exc)
        logger.warning("CLI merge fallback failed: %s", exc)
        return False


# ── Main entry point ─────────────────────────────────────


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
    issue_data = issue if issue is not None else await get_issue(repo, issue_number)

    if not is_trusted_content_author(issue_data):
        raise ValueError(
            f"Issue #{issue_number} author is not trusted — "
            "refusing to inject untrusted content into CLI prompt"
        )

    acc = _TokenAccumulator()
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
            stage="implement",
            model=model,
            effort=reasoning_effort,
            github_token=token,
        )
        acc.add_result(cli_result)

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

        if not pr_data:
            elapsed = _monotonic() - start
            result = _build_result(None, elapsed, acc, cli_result, repo, model, reasoning_effort)
            return result

        await lock_pr(repo, pr_data.number)

        # ── Step 2: Review-fix loop (skip if already merged) ──
        if not _is_merged(pr_data):
            ctx = _LoopContext(
                worktree_path=worktree_path,
                repo=repo,
                model=model,
                reasoning_effort=reasoning_effort,
                token=token,
                implement_session_id=cli_result.session_id,
            )
            approved = await _run_review_fix_loop(
                ctx, pr_data.number, issue_data, issue_number, acc
            )

            if approved:
                await _try_merge(ctx, pr_data.number, branch_name, acc)
                pr_data = await find_pr_by_branch(repo, branch_name)
            else:
                await safe_comment(
                    repo,
                    pr_data.number,
                    f"⚠️ Review-fix loop exhausted ({MAX_REVIEW_FIX_ROUNDS} rounds) "
                    "with unresolved threads — needs human review.",
                )

        elapsed = _monotonic() - start
        result = _build_result(pr_data, elapsed, acc, cli_result, repo, model, reasoning_effort)

        if result.merged:
            await close_issue(repo, issue_number)

        return result

    except TaskError:
        raise
    except Exception as exc:
        raise TaskError(
            str(exc),
            premium_requests=acc.premium_requests,
            input_tokens=acc.input_tokens,
            output_tokens=acc.output_tokens,
            cached_tokens=acc.cached_tokens,
            reasoning_tokens=acc.reasoning_tokens,
        ) from exc

    finally:
        await cleanup_branch_worktree(branch_name)
