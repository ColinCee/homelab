"""Issue implementation orchestrator — turns issues into PRs via Copilot CLI."""

import asyncio
import contextlib
import logging
import time

import httpx

from copilot import TaskError, run_copilot
from git import cleanup_branch_worktree, commit_and_push, create_branch_worktree
from github import (
    TRUSTED_ROLES,
    bot_login,
    comment_on_issue,
    create_pull_request,
    get_commit_ci_status,
    get_issue,
    get_pr,
    get_token,
    merge_pull_request,
)
from review import review_pr

logger = logging.getLogger(__name__)

MAX_FIX_ITERATIONS = 3
MERGE_READY_TIMEOUT_SECONDS = 15 * 60
MERGE_POLL_INTERVAL_SECONDS = 10

STATUS_EMOJI = {
    "complete": "✅",
    "partial": "⚠️",
    "max_iterations": "⚠️",
    "failed": "❌",
}


def _format_stats_comment(result: dict) -> str:
    """Format a lifecycle stats summary for posting on the PR."""
    status = result.get("status", "unknown")
    emoji = STATUS_EMOJI.get(status, "❓")
    lines = [f"{emoji} **Implementation {status}**"]

    rounds = result.get("review_rounds", 0)
    lines.append(f"- Review rounds: {rounds}/{MAX_FIX_ITERATIONS + 1}")

    premium = result.get("premium_requests", 0)
    if premium:
        lines.append(f"- Premium requests: {premium}")

    elapsed = result.get("elapsed_seconds", 0)
    if elapsed:
        minutes, secs = divmod(int(elapsed), 60)
        lines.append(f"- Elapsed: {minutes}m {secs}s")

    if result.get("merged"):
        lines.append(f"- Merge: squash → `{result.get('merge_commit_sha', '?')}`")

    session_id = result.get("session_id")
    if session_id:
        lines.append(f"- Session: `{session_id}`")

    error = result.get("error")
    if error:
        lines.append(f"\n{error}")

    return "\n".join(lines)


IMPLEMENT_PROMPT_TEMPLATE = """\
Implement the following GitHub issue in {repo}.

## Issue #{issue_number}: {title}

{body}

Use the bot-implement skill for guidelines on how to make changes.
"""

FIX_PROMPT_TEMPLATE = """\
The automated review found issues with your implementation of #{issue_number}. \
Fix all reported problems. Use the skill `bot-implement` for guidance.

## Review Findings

{threads}

## After Fixing

Each fix round costs time and tokens — aim for zero new issues.

1. **Self-review your fix for second-order effects.** If you changed control flow, \
error handling, or added new API calls, audit the surrounding code for issues \
your fix may have introduced (missing pagination, unhandled errors, new edge cases).
2. **Run `mise run ci`** to validate lint, typecheck, and tests pass.
3. **Check for committed artifacts.** Run `git status` and ensure no runtime files \
(`.cleanup-after`, `.copilot-session.md`, `.copilot/`) would be staged by `git add -A`.
"""


def _lifecycle_result(
    *,
    status: str,
    pr_number: int,
    pr_url: str,
    commit_sha: str,
    review_rounds: int,
    start: float,
    premium_requests: int,
    error: str | None = None,
    merged: bool = False,
    merge_commit_sha: str | None = None,
    mergeable_state: str | None = None,
    ci_status: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Build a consistent lifecycle status payload."""
    result: dict = {
        "status": status,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "commit_sha": commit_sha,
        "review_rounds": review_rounds,
        "elapsed_seconds": time.monotonic() - start,
        "premium_requests": premium_requests,
        "merged": merged,
    }
    if error is not None:
        result["error"] = error
    if merge_commit_sha:
        result["merge_commit_sha"] = merge_commit_sha
        result["merge_method"] = "squash"
    if mergeable_state:
        result["mergeable_state"] = mergeable_state
    if ci_status:
        result["ci_status"] = ci_status
    if session_id:
        result["session_id"] = session_id
    return result


async def _merge_when_eligible(
    *,
    repo: str,
    pr_number: int,
    pr_url: str,
    branch_name: str,
    commit_sha: str,
    review_rounds: int,
    start: float,
    premium_requests: int,
    session_id: str | None = None,
) -> dict:
    """Wait for a clean, green PR and squash-merge it when GitHub allows."""
    deadline = time.monotonic() + MERGE_READY_TIMEOUT_SECONDS
    last_wait_reason = "GitHub is still calculating mergeability"

    while time.monotonic() < deadline:
        mergeable_state = "unknown"
        ci_status: str | None = None

        try:
            pr_data = await get_pr(repo, pr_number)
            mergeable_state = pr_data.get("mergeable_state", "unknown")

            if pr_data.get("merged"):
                return _lifecycle_result(
                    status="complete",
                    pr_number=pr_number,
                    pr_url=pr_url,
                    commit_sha=commit_sha,
                    review_rounds=review_rounds,
                    start=start,
                    premium_requests=premium_requests,
                    session_id=session_id,
                    merged=True,
                    merge_commit_sha=pr_data.get("merge_commit_sha"),
                    mergeable_state=mergeable_state,
                )

            if pr_data.get("state") != "open":
                return _lifecycle_result(
                    status="partial",
                    pr_number=pr_number,
                    pr_url=pr_url,
                    commit_sha=commit_sha,
                    review_rounds=review_rounds,
                    start=start,
                    premium_requests=premium_requests,
                    session_id=session_id,
                    error="PR closed without merge — needs manual attention",
                    mergeable_state=mergeable_state,
                )

            if pr_data.get("draft"):
                return _lifecycle_result(
                    status="partial",
                    pr_number=pr_number,
                    pr_url=pr_url,
                    commit_sha=commit_sha,
                    review_rounds=review_rounds,
                    start=start,
                    premium_requests=premium_requests,
                    session_id=session_id,
                    error="PR is still draft — needs manual attention",
                    mergeable_state=mergeable_state,
                )

            if pr_data.get("user", {}).get("login") != bot_login():
                return _lifecycle_result(
                    status="partial",
                    pr_number=pr_number,
                    pr_url=pr_url,
                    commit_sha=commit_sha,
                    review_rounds=review_rounds,
                    start=start,
                    premium_requests=premium_requests,
                    session_id=session_id,
                    error="PR is not bot-authored — refusing to auto-merge",
                    mergeable_state=mergeable_state,
                )

            current_head = pr_data.get("head", {})
            if current_head.get("ref") != branch_name:
                return _lifecycle_result(
                    status="partial",
                    pr_number=pr_number,
                    pr_url=pr_url,
                    commit_sha=commit_sha,
                    review_rounds=review_rounds,
                    start=start,
                    premium_requests=premium_requests,
                    session_id=session_id,
                    error="PR head no longer matches the implementation branch "
                    "— needs manual attention",
                    mergeable_state=mergeable_state,
                )

            current_sha = current_head.get("sha")
            if current_sha != commit_sha:
                return _lifecycle_result(
                    status="partial",
                    pr_number=pr_number,
                    pr_url=pr_url,
                    commit_sha=commit_sha,
                    review_rounds=review_rounds,
                    start=start,
                    premium_requests=premium_requests,
                    session_id=session_id,
                    error="PR head changed after review — needs manual attention",
                    mergeable_state=mergeable_state,
                )

            ci_result = await get_commit_ci_status(repo, commit_sha)
            ci_status = ci_result.get("state", "none")
            ci_description = ci_result.get("description", "Waiting for CI")

            if ci_status == "failure":
                return _lifecycle_result(
                    status="partial",
                    pr_number=pr_number,
                    pr_url=pr_url,
                    commit_sha=commit_sha,
                    review_rounds=review_rounds,
                    start=start,
                    premium_requests=premium_requests,
                    session_id=session_id,
                    error=f"{ci_description} — needs manual attention",
                    mergeable_state=mergeable_state,
                    ci_status=ci_status,
                )

            if pr_data.get("mergeable") is None or mergeable_state == "unknown":
                last_wait_reason = "GitHub is still calculating mergeability"
                await asyncio.sleep(MERGE_POLL_INTERVAL_SECONDS)
                continue

            if ci_status in ("pending", "none"):
                last_wait_reason = ci_description
                await asyncio.sleep(MERGE_POLL_INTERVAL_SECONDS)
                continue

            if not pr_data.get("mergeable"):
                return _lifecycle_result(
                    status="partial",
                    pr_number=pr_number,
                    pr_url=pr_url,
                    commit_sha=commit_sha,
                    review_rounds=review_rounds,
                    start=start,
                    premium_requests=premium_requests,
                    session_id=session_id,
                    error=f"PR is not mergeable ({mergeable_state}) — needs manual attention",
                    mergeable_state=mergeable_state,
                    ci_status=ci_status,
                )

            merge_result = await merge_pull_request(repo, pr_number, sha=commit_sha)
            if not merge_result.get("merged"):
                message = merge_result.get("message", "GitHub rejected squash merge")
                if mergeable_state != "clean":
                    last_wait_reason = f"GitHub rejected squash merge: {message}"
                    await asyncio.sleep(MERGE_POLL_INTERVAL_SECONDS)
                    continue

                return _lifecycle_result(
                    status="partial",
                    pr_number=pr_number,
                    pr_url=pr_url,
                    commit_sha=commit_sha,
                    review_rounds=review_rounds,
                    start=start,
                    premium_requests=premium_requests,
                    session_id=session_id,
                    error=f"GitHub rejected squash merge: {message}",
                    mergeable_state=mergeable_state,
                    ci_status=ci_status,
                )

            return _lifecycle_result(
                status="complete",
                pr_number=pr_number,
                pr_url=pr_url,
                commit_sha=commit_sha,
                review_rounds=review_rounds,
                start=start,
                premium_requests=premium_requests,
                session_id=session_id,
                merged=True,
                merge_commit_sha=merge_result.get("sha"),
                mergeable_state=mergeable_state,
                ci_status=ci_status,
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.warning("Merge polling failed for %s PR #%d: %s", repo, pr_number, exc)
            return _lifecycle_result(
                status="partial",
                pr_number=pr_number,
                pr_url=pr_url,
                commit_sha=commit_sha,
                review_rounds=review_rounds,
                start=start,
                premium_requests=premium_requests,
                session_id=session_id,
                error=f"GitHub merge polling failed: {exc} — needs manual attention",
                mergeable_state=mergeable_state,
                ci_status=ci_status,
            )

    return _lifecycle_result(
        status="partial",
        pr_number=pr_number,
        pr_url=pr_url,
        commit_sha=commit_sha,
        review_rounds=review_rounds,
        start=start,
        premium_requests=premium_requests,
        session_id=session_id,
        error=f"Timed out waiting for merge eligibility: {last_wait_reason}",
    )


async def implement_issue(
    *,
    repo: str,
    issue_number: int,
    model: str = "gpt-5.4",
    reasoning_effort: str = "high",
) -> dict:
    """Implement a GitHub issue: branch → CLI → push → PR → review/fix → merge."""
    logger.info("Implementing issue %s#%d", repo, issue_number)
    start = time.monotonic()
    repo_url = f"https://github.com/{repo}.git"
    branch_name = f"agent/issue-{issue_number}"

    token = await get_token()
    issue = await get_issue(repo, issue_number)

    # Trust boundary: only implement issues from trusted authors to prevent
    # prompt injection via attacker-controlled issue bodies
    author_role = issue.get("author_association", "NONE")
    if author_role not in TRUSTED_ROLES:
        raise ValueError(
            f"Issue #{issue_number} author has role '{author_role}' — "
            f"only {TRUSTED_ROLES} are trusted for autonomous implementation"
        )

    total_premium_requests = 0
    pr_number = None
    lifecycle_result: dict | None = None

    try:
        worktree_path = await create_branch_worktree(branch_name, repo_url)

        prompt = IMPLEMENT_PROMPT_TEMPLATE.format(
            repo=repo,
            issue_number=issue_number,
            title=issue["title"],
            body=issue.get("body") or "(no description)",
        )

        # Agent gets NO GitHub API access — it only edits local files.
        # The orchestrator handles all git/GitHub operations with the App token.
        result = await run_copilot(
            worktree_path,
            prompt,
            model=model,
            effort=reasoning_effort,
        )
        total_premium_requests += result.total_premium_requests
        implement_session_id = result.session_id

        # Post-CLI operations can fail — preserve premium request count for metrics
        try:
            sha = await commit_and_push(
                worktree_path,
                message=f"feat: implement #{issue_number} — {issue['title']}",
                token=token,
                repo=repo,
                branch=branch_name,
            )

            pr = await create_pull_request(
                repo,
                title=f"feat: {issue['title']}",
                body=f"Closes #{issue_number}.\n\nAutonomously generated by the homelab agent.",
                head=branch_name,
                base="main",
            )
        except Exception as exc:
            raise TaskError(str(exc), premium_requests=total_premium_requests) from exc

        pr_number = pr["number"]
        pr_url = pr["html_url"]

        # Review+fix loop — review first, then fix if needed.
        # We allow MAX_FIX_ITERATIONS fix attempts. Each fix is followed by a
        # re-review, so we run up to MAX_FIX_ITERATIONS + 1 review rounds
        # (initial + one after each fix).
        previous_review_threads = ""
        for review_round in range(MAX_FIX_ITERATIONS + 1):
            logger.info(
                "Review round %d/%d for %s#%d (PR #%d)",
                review_round + 1,
                MAX_FIX_ITERATIONS + 1,
                repo,
                issue_number,
                pr_number,
            )

            try:
                review_result = await review_pr(
                    repo=repo,
                    pr_number=pr_number,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    previous_comments=previous_review_threads,
                )
            except TaskError as exc:
                total_premium_requests += exc.premium_requests
                logger.warning("Review failed on round %d: %s", review_round + 1, exc)
                lifecycle_result = _lifecycle_result(
                    status="partial",
                    pr_number=pr_number,
                    pr_url=pr_url,
                    commit_sha=sha,
                    review_rounds=review_round + 1,
                    start=start,
                    premium_requests=total_premium_requests,
                    session_id=implement_session_id,
                    error=f"Review failed on round {review_round + 1}: {exc}",
                )
                return lifecycle_result

            total_premium_requests += review_result.get("premium_requests", 0)
            original_event = review_result.get("original_event", "COMMENT")

            if original_event != "REQUEST_CHANGES":
                logger.info(
                    "Review passed (%s) on round %d for %s#%d",
                    original_event,
                    review_round + 1,
                    repo,
                    issue_number,
                )
                lifecycle_result = await _merge_when_eligible(
                    repo=repo,
                    pr_number=pr_number,
                    pr_url=pr_url,
                    branch_name=branch_name,
                    commit_sha=sha,
                    review_rounds=review_round + 1,
                    start=start,
                    premium_requests=total_premium_requests,
                    session_id=implement_session_id,
                )
                return lifecycle_result

            # REQUEST_CHANGES — check if we have fix attempts remaining
            if review_round == MAX_FIX_ITERATIONS:
                # Final review after last fix still found issues
                break

            if not implement_session_id:
                logger.warning(
                    "Cannot resume session — no session ID captured. Stopping after review."
                )
                lifecycle_result = _lifecycle_result(
                    status="partial",
                    pr_number=pr_number,
                    pr_url=pr_url,
                    commit_sha=sha,
                    review_rounds=review_round + 1,
                    start=start,
                    premium_requests=total_premium_requests,
                    session_id=implement_session_id,
                    error="Review requested changes but session resumption unavailable",
                )
                return lifecycle_result

            review_threads = review_result.get("review_threads", "")
            if not review_threads:
                logger.warning("REQUEST_CHANGES but no inline findings to fix")
                lifecycle_result = _lifecycle_result(
                    status="partial",
                    pr_number=pr_number,
                    pr_url=pr_url,
                    commit_sha=sha,
                    review_rounds=review_round + 1,
                    start=start,
                    premium_requests=total_premium_requests,
                    session_id=implement_session_id,
                    error="Review requested changes but no inline comments "
                    "were posted — needs manual attention",
                )
                return lifecycle_result

            fix_prompt = FIX_PROMPT_TEMPLATE.format(
                issue_number=issue_number,
                threads=review_threads,
            )

            try:
                fix_result = await run_copilot(
                    worktree_path,
                    fix_prompt,
                    model=model,
                    effort=reasoning_effort,
                    session_id=implement_session_id,
                )
                total_premium_requests += fix_result.total_premium_requests
                if fix_result.session_id:
                    implement_session_id = fix_result.session_id
            except TaskError as exc:
                total_premium_requests += exc.premium_requests
                lifecycle_result = _lifecycle_result(
                    status="failed",
                    pr_number=pr_number,
                    pr_url=pr_url,
                    commit_sha=sha,
                    review_rounds=review_round + 1,
                    start=start,
                    premium_requests=total_premium_requests,
                    session_id=implement_session_id,
                    error=f"Fix failed on round {review_round + 1}: {exc}",
                )
                raise TaskError(
                    f"Fix failed on round {review_round + 1}: {exc}",
                    premium_requests=total_premium_requests,
                ) from exc

            try:
                sha = await commit_and_push(
                    worktree_path,
                    message=f"fix: address review feedback on #{issue_number} "
                    f"(round {review_round + 1})",
                    token=token,
                    repo=repo,
                    branch=branch_name,
                )
                previous_review_threads = review_threads
            except RuntimeError as exc:
                if "No changes to commit" in str(exc):
                    logger.warning(
                        "No changes after fix round %d — needs manual attention",
                        review_round + 1,
                    )
                    lifecycle_result = _lifecycle_result(
                        status="partial",
                        pr_number=pr_number,
                        pr_url=pr_url,
                        commit_sha=sha,
                        review_rounds=review_round + 1,
                        start=start,
                        premium_requests=total_premium_requests,
                        session_id=implement_session_id,
                        error="Fix produced no changes — remaining findings need manual attention",
                    )
                    return lifecycle_result
                lifecycle_result = _lifecycle_result(
                    status="failed",
                    pr_number=pr_number,
                    pr_url=pr_url,
                    commit_sha=sha,
                    review_rounds=review_round + 1,
                    start=start,
                    premium_requests=total_premium_requests,
                    session_id=implement_session_id,
                    error=f"Commit/push failed on round {review_round + 1}: {exc}",
                )
                raise TaskError(str(exc), premium_requests=total_premium_requests) from exc
            except Exception as exc:
                lifecycle_result = _lifecycle_result(
                    status="failed",
                    pr_number=pr_number,
                    pr_url=pr_url,
                    commit_sha=sha,
                    review_rounds=review_round + 1,
                    start=start,
                    premium_requests=total_premium_requests,
                    session_id=implement_session_id,
                    error=f"Commit/push failed on round {review_round + 1}: {exc}",
                )
                raise TaskError(str(exc), premium_requests=total_premium_requests) from exc

        # Exhausted all fix iterations — last review still requested changes
        logger.warning(
            "Hit max fix iterations (%d) for %s#%d", MAX_FIX_ITERATIONS, repo, issue_number
        )

        lifecycle_result = _lifecycle_result(
            status="max_iterations",
            pr_number=pr_number,
            pr_url=pr_url,
            commit_sha=sha,
            review_rounds=MAX_FIX_ITERATIONS + 1,
            start=start,
            premium_requests=total_premium_requests,
            session_id=implement_session_id,
        )
        return lifecycle_result

    finally:
        if lifecycle_result and pr_number:
            with contextlib.suppress(Exception):
                await comment_on_issue(repo, pr_number, _format_stats_comment(lifecycle_result))
        await cleanup_branch_worktree(branch_name)
