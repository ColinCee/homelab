"""Issue implementation orchestrator — turns issues into PRs via Copilot CLI."""

import asyncio
import contextlib
import logging
import re
import time
from pathlib import Path

import httpx

from copilot import CLIResult, TaskError, run_copilot
from git import (
    RebaseConflictError,
    cleanup_branch_worktree,
    commit_and_push,
    create_branch_worktree,
    rebase_onto_main,
)
from github import (
    TRUSTED_ROLES,
    bot_login,
    close_issue,
    comment_on_issue,
    create_pull_request,
    get_commit_ci_status,
    get_issue,
    get_pr,
    get_token,
    merge_pull_request,
    update_comment,
)
from review import review_pr

logger = logging.getLogger(__name__)

MAX_REVIEW_ROUNDS = 2
MERGE_READY_TIMEOUT_SECONDS = 15 * 60
MERGE_POLL_INTERVAL_SECONDS = 10
REBASE_HEAD_PROPAGATION_GRACE_SECONDS = 3 * MERGE_POLL_INTERVAL_SECONDS

STATUS_EMOJI = {
    "complete": "✅",
    "partial": "⚠️",
    "failed": "❌",
}


def _monotonic() -> float:
    return time.monotonic()


def _format_stage_stats(
    *,
    premium_requests: int = 0,
    elapsed_seconds: float = 0,
    api_time_seconds: int = 0,
    effort: str = "",
    models: dict | None = None,
    tokens_line: str = "",
) -> str:
    """Format a compact stats footer for a lifecycle stage comment."""
    parts = []
    if premium_requests:
        parts.append(f"💰 {premium_requests} premium")
    if elapsed_seconds:
        minutes, secs = divmod(int(elapsed_seconds), 60)
        time_str = f"⏱️ {minutes}m {secs}s"
        if api_time_seconds:
            am, as_ = divmod(api_time_seconds, 60)
            time_str += f" (API: {am}m {as_}s)"
        parts.append(time_str)
    if effort:
        parts.append(f"🧠 {effort}")
    if models:
        for model_name, detail in models.items():
            clean = re.sub(r"\s*\(Est\..*?\)", "", detail).strip().rstrip(",")
            parts.append(f"🤖 {model_name}: {clean}")
    elif tokens_line:
        parts.append(f"📊 {tokens_line}")
    return " · ".join(parts)


def _cli_stage_stats(result: CLIResult, effort: str = "") -> str:
    """Format stats from a CLIResult."""
    return _format_stage_stats(
        premium_requests=result.total_premium_requests,
        elapsed_seconds=result.session_time_seconds,
        api_time_seconds=result.api_time_seconds,
        effort=effort,
        models=result.models,
        tokens_line=result.tokens_line,
    )


def _format_stats_comment(result: dict) -> str:
    """Format a lifecycle stats summary for posting on the PR."""
    status = result.get("status", "unknown")
    emoji = STATUS_EMOJI.get(status, "❓")
    lines = [f"{emoji} **Implementation {status}**"]

    rounds = result.get("review_rounds", 0)
    lines.append(f"- Review: {rounds} round{'s' if rounds != 1 else ''} (max {MAX_REVIEW_ROUNDS})")

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

{round_context}

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
        "elapsed_seconds": _monotonic() - start,
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
    worktree_path: Path,
    token: str,
    repo_url: str,
    commit_sha: str,
    review_rounds: int,
    start: float,
    premium_requests: int,
    review_progress_id: int | None = None,
    session_id: str | None = None,
) -> dict:
    """Wait for a clean, green PR and squash-merge it when GitHub allows."""
    deadline = _monotonic() + MERGE_READY_TIMEOUT_SECONDS
    last_wait_reason = "GitHub is still calculating mergeability"
    rebase_attempted = False
    previous_commit_sha: str | None = None
    head_sync_deadline: float | None = None

    while _monotonic() < deadline:
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
                if (
                    previous_commit_sha is not None
                    and current_sha == previous_commit_sha
                    and head_sync_deadline is not None
                ):
                    if _monotonic() < head_sync_deadline:
                        last_wait_reason = "Waiting for GitHub to reflect rebased PR head"
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
                        error="PR head did not update to the rebased commit"
                        " — needs manual attention",
                        mergeable_state=mergeable_state,
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
                    error="PR head changed after review — needs manual attention",
                    mergeable_state=mergeable_state,
                )
            previous_commit_sha = None
            head_sync_deadline = None

            ci_result = await get_commit_ci_status(repo, commit_sha)
            ci_status = ci_result.get("state", "none")

            if pr_data.get("mergeable") is None or mergeable_state == "unknown":
                last_wait_reason = "GitHub is still calculating mergeability"
                await asyncio.sleep(MERGE_POLL_INTERVAL_SECONDS)
                continue

            if not pr_data.get("mergeable"):
                if mergeable_state == "dirty" and not rebase_attempted:
                    rebase_attempted = True
                    with contextlib.suppress(Exception):
                        if review_progress_id is not None:
                            await update_comment(
                                repo, review_progress_id, "🔄 Rebasing onto main..."
                            )
                    try:
                        previous_commit_sha = commit_sha
                        commit_sha = await rebase_onto_main(
                            worktree_path,
                            repo_url=repo_url,
                            token=token,
                            repo=repo,
                            branch=branch_name,
                        )
                        head_sync_deadline = _monotonic() + REBASE_HEAD_PROPAGATION_GRACE_SECONDS
                        last_wait_reason = "Rebased onto main — waiting for CI"
                        await asyncio.sleep(MERGE_POLL_INTERVAL_SECONDS)
                        continue
                    except RebaseConflictError as exc:
                        return _lifecycle_result(
                            status="partial",
                            pr_number=pr_number,
                            pr_url=pr_url,
                            commit_sha=commit_sha,
                            review_rounds=review_rounds,
                            start=start,
                            premium_requests=premium_requests,
                            session_id=session_id,
                            error="Rebase onto main failed with conflicts"
                            f" — needs manual resolution: {exc}",
                            mergeable_state=mergeable_state,
                            ci_status=ci_status,
                        )
                last_wait_reason = f"PR is not mergeable ({mergeable_state}) — waiting"
                await asyncio.sleep(MERGE_POLL_INTERVAL_SECONDS)
                continue

            # Let GitHub's merge API be the sole authority on whether the PR
            # can be merged. We don't pre-filter on CI status because
            # optional/informational checks would block us — GitHub's API
            # enforces required checks and rejects if they're not satisfied.
            merge_result = await merge_pull_request(repo, pr_number, sha=commit_sha)
            if not merge_result.get("merged"):
                message = merge_result.get("message", "GitHub rejected squash merge")
                last_wait_reason = f"Merge not accepted: {message}"
                await asyncio.sleep(MERGE_POLL_INTERVAL_SECONDS)
                continue

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
    issue: dict | None = None,
) -> dict:
    """Implement a GitHub issue: branch → CLI → push → PR → review/fix → merge."""
    logger.info("Implementing issue %s#%d", repo, issue_number)
    start = _monotonic()
    repo_url = f"https://github.com/{repo}.git"
    branch_name = f"agent/issue-{issue_number}"

    token = await get_token()
    issue_data = issue
    if issue_data is None:
        issue_data = await get_issue(repo, issue_number)

    # Trust boundary: only implement issues from trusted authors to prevent
    # prompt injection via attacker-controlled issue bodies
    author_role = issue_data.get("author_association", "NONE")
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
            title=issue_data["title"],
            body=issue_data.get("body") or "(no description)",
        )

        # CLI gets full repo access via GH_TOKEN for git push, PR creation,
        # review posting, and merge operations.
        result = await run_copilot(
            worktree_path,
            prompt,
            model=model,
            effort=reasoning_effort,
            github_token=token,
        )
        total_premium_requests += result.total_premium_requests
        implement_session_id = result.session_id

        # Post-CLI operations can fail — preserve premium request count for metrics
        try:
            sha = await commit_and_push(
                worktree_path,
                message=f"feat: implement #{issue_number} — {issue_data['title']}",
                token=token,
                repo=repo,
                branch=branch_name,
            )

            pr = await create_pull_request(
                repo,
                title=f"feat: {issue_data['title']}",
                body=f"Closes #{issue_number}.\n\nAutonomously generated by the homelab agent.",
                head=branch_name,
                base="main",
            )
        except Exception as exc:
            raise TaskError(str(exc), premium_requests=total_premium_requests) from exc

        pr_number = pr["number"]
        pr_url = pr["html_url"]
        review_session_id: str | None = None

        with contextlib.suppress(Exception):
            impl_stats = _cli_stage_stats(result, effort=reasoning_effort)
            await comment_on_issue(
                repo,
                pr_number,
                f"🏗️ **Implementation complete** — PR #{pr_number} created\n{impl_stats}",
            )

        # Review/fix loop — up to MAX_REVIEW_ROUNDS rounds.
        # Each stage posts "in progress", then edits the same comment with
        # stats when done. Separate comments per stage preserve history.
        review_rounds = 0
        for round_num in range(1, MAX_REVIEW_ROUNDS + 1):
            round_label = f"round {round_num}/{MAX_REVIEW_ROUNDS}"
            review_comment_id: int | None = None
            with contextlib.suppress(Exception):
                review_comment_id = await comment_on_issue(
                    repo,
                    pr_number,
                    f"🔄 **Review {round_label}** in progress...",
                )

            try:
                review_result = await review_pr(
                    repo=repo,
                    pr_number=pr_number,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    session_id=review_session_id,
                )
                total_premium_requests += review_result.get("premium_requests", 0)
                review_session_id = review_result.get("session_id")
                original_event = review_result.get("original_event", "COMMENT")
                review_rounds += 1

                with contextlib.suppress(Exception):
                    review_stats = _format_stage_stats(
                        premium_requests=review_result.get("premium_requests", 0),
                        elapsed_seconds=review_result.get("elapsed_seconds", 0),
                        api_time_seconds=review_result.get("api_time_seconds", 0),
                        effort=review_result.get("reasoning_effort", ""),
                        models=review_result.get("models"),
                        tokens_line=review_result.get("tokens_line", ""),
                    )
                    verdict = (
                        "✅ approved" if original_event == "APPROVE" else "📋 changes requested"
                    )
                    body = f"🔍 **Review {round_label}** — {verdict}\n{review_stats}"
                    if review_comment_id:
                        await update_comment(repo, review_comment_id, body)
                    else:
                        await comment_on_issue(repo, pr_number, body)

                logger.info(
                    "Review round %d/%d (%s) for %s#%d (PR #%d)",
                    round_num,
                    MAX_REVIEW_ROUNDS,
                    original_event,
                    repo,
                    issue_number,
                    pr_number,
                )
            except TaskError as exc:
                total_premium_requests += exc.premium_requests
                review_rounds += 1
                logger.warning(
                    "Review round %d failed for %s#%d: %s — proceeding to merge",
                    round_num,
                    repo,
                    issue_number,
                    exc,
                )
                break  # Review errored — skip fix, go to merge

            if original_event != "REQUEST_CHANGES":
                break  # Approved or commented — no fix needed

            # Fix pass
            fix_comment_id: int | None = None
            with contextlib.suppress(Exception):
                fix_comment_id = await comment_on_issue(
                    repo,
                    pr_number,
                    f"🔧 **Fixing** review findings ({round_label})...",
                )

            review_threads = review_result.get("review_threads", "")
            if not review_threads or not implement_session_id:
                reason = "no session ID" if not implement_session_id else "no inline findings"
                logger.error(
                    "Cannot fix review findings (%s) for %s#%d — needs manual attention",
                    reason,
                    repo,
                    issue_number,
                )
                lifecycle_result = _lifecycle_result(
                    status="partial",
                    pr_number=pr_number,
                    pr_url=pr_url,
                    commit_sha=sha,
                    review_rounds=review_rounds,
                    start=start,
                    premium_requests=total_premium_requests,
                    session_id=implement_session_id,
                    error=f"Review requested changes but fix cannot run ({reason})"
                    " — needs manual attention",
                )
                return lifecycle_result

            is_final_round = round_num == MAX_REVIEW_ROUNDS
            round_context = (
                "This is your final fix round — there is no re-review after this. Get it right."
                if is_final_round
                else "After this fix, one more review round remains. Focus on correctness."
            )
            fix_prompt = FIX_PROMPT_TEMPLATE.format(
                issue_number=issue_number,
                threads=review_threads,
                round_context=round_context,
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
                total_premium_requests += fix_result.total_premium_requests
                if fix_result.session_id:
                    implement_session_id = fix_result.session_id

                with contextlib.suppress(Exception):
                    fix_stats = _cli_stage_stats(fix_result, effort=reasoning_effort)
                    body = f"🔧 **Fix {round_label}** complete\n{fix_stats}"
                    if fix_comment_id:
                        await update_comment(repo, fix_comment_id, body)
                    else:
                        await comment_on_issue(repo, pr_number, body)
            except TaskError as exc:
                total_premium_requests += exc.premium_requests
                lifecycle_result = _lifecycle_result(
                    status="failed",
                    pr_number=pr_number,
                    pr_url=pr_url,
                    commit_sha=sha,
                    review_rounds=review_rounds,
                    start=start,
                    premium_requests=total_premium_requests,
                    session_id=implement_session_id,
                    error=f"Fix round {round_num} failed: {exc}",
                )
                raise TaskError(
                    f"Fix round {round_num} failed: {exc}",
                    premium_requests=total_premium_requests,
                ) from exc

            try:
                sha = await commit_and_push(
                    worktree_path,
                    message=f"fix: address review feedback (round {round_num}) on #{issue_number}",
                    token=token,
                    repo=repo,
                    branch=branch_name,
                )
            except RuntimeError as exc:
                if "No changes to commit" in str(exc):
                    logger.error(
                        "Fix round %d produced no changes for %s#%d"
                        " — review found issues but nothing was fixed",
                        round_num,
                        repo,
                        issue_number,
                    )
                    lifecycle_result = _lifecycle_result(
                        status="partial",
                        pr_number=pr_number,
                        pr_url=pr_url,
                        commit_sha=sha,
                        review_rounds=review_rounds,
                        start=start,
                        premium_requests=total_premium_requests,
                        session_id=implement_session_id,
                        error=f"Fix round {round_num} produced no changes"
                        " — review issues unresolved, needs manual attention",
                    )
                    return lifecycle_result
                else:
                    lifecycle_result = _lifecycle_result(
                        status="failed",
                        pr_number=pr_number,
                        pr_url=pr_url,
                        commit_sha=sha,
                        review_rounds=review_rounds,
                        start=start,
                        premium_requests=total_premium_requests,
                        session_id=implement_session_id,
                        error=f"Commit/push failed: {exc}",
                    )
                    raise TaskError(str(exc), premium_requests=total_premium_requests) from exc

        review_progress_id: int | None = None
        with contextlib.suppress(Exception):
            rounds_label = f"{review_rounds} round{'s' if review_rounds != 1 else ''}"
            review_progress_id = await comment_on_issue(
                repo,
                pr_number,
                f"⏳ **Review complete** ({rounds_label}) — waiting for merge...",
            )

        lifecycle_result = await _merge_when_eligible(
            repo=repo,
            pr_number=pr_number,
            pr_url=pr_url,
            branch_name=branch_name,
            worktree_path=worktree_path,
            token=token,
            repo_url=repo_url,
            commit_sha=sha,
            review_rounds=review_rounds,
            start=start,
            premium_requests=total_premium_requests,
            review_progress_id=review_progress_id,
            session_id=implement_session_id,
        )

        if lifecycle_result.get("status") == "complete":
            with contextlib.suppress(Exception):
                await close_issue(repo, issue_number)

        return lifecycle_result

    finally:
        if lifecycle_result and pr_number:
            with contextlib.suppress(Exception):
                await comment_on_issue(repo, pr_number, _format_stats_comment(lifecycle_result))
        await cleanup_branch_worktree(branch_name)
