"""Ephemeral worker entrypoint — runs a single agent task then exits.

Spawned by the API via `docker run ... python -m worker`. Reads task
parameters from environment variables, calls the appropriate orchestrator,
posts progress/result comments to GitHub, and writes a JSON result to stdout
for the API's monitor coroutine to parse.
"""

import asyncio
import logging
import sys

from implement import implement_issue
from logging_config import configure_logging, set_task_context
from models import TaskResult
from review import review_pr
from runtime_env import WorkerSettings
from services.copilot import TaskError
from services.github import (
    comment_on_issue,
    find_issue_comment_by_body_prefix,
    get_issue,
    safe_comment,
    set_token,
    update_comment,
)
from stats import STATUS_EMOJI, task_stage_stats

configure_logging()
logger = logging.getLogger(__name__)

REVIEW_PROGRESS_PREFIX = "🔄 Review in progress for PR #"
IMPLEMENT_PROGRESS_PREFIX = "🔄 Implementing #"


async def _start_progress_comment(
    repo: str, issue_number: int, *, body: str, body_prefix: str
) -> int | None:
    """Post or update a progress comment on an issue/PR."""
    try:
        comment_id = await find_issue_comment_by_body_prefix(repo, issue_number, body_prefix)
        if comment_id is not None:
            await update_comment(repo, comment_id, body)
            return comment_id
        return await comment_on_issue(repo, issue_number, body)
    except Exception:
        logger.warning(
            "Failed to start progress comment on %s#%d (%s)",
            repo,
            issue_number,
            body_prefix,
            exc_info=True,
        )
    return None


async def _update_progress_comment(repo: str, comment_id: int | None, body: str) -> None:
    if comment_id is None:
        return
    try:
        await update_comment(repo, comment_id, body)
    except Exception:
        logger.warning(
            "Failed to update progress comment %d on %s",
            comment_id,
            repo,
            exc_info=True,
        )


def _format_implement_result_comment(result: TaskResult, *, effort: str) -> str:
    summary = _implement_result_summary(result)
    stats = task_stage_stats(result, effort=effort)
    return "\n".join(part for part in (summary, stats) if part)


def _implement_result_summary(result: TaskResult) -> str:
    if result.status == "complete":
        if result.pr_number is not None and result.pr_url:
            if result.merged:
                return f"✅ PR #{result.pr_number} merged — {result.pr_url}"
            if result.auto_merge:
                return f"✅ PR #{result.pr_number} created (auto-merge enabled) — {result.pr_url}"
            return f"✅ PR #{result.pr_number} created — {result.pr_url}"
        return "✅ Implementation complete"

    if result.status == "partial":
        summary = "⚠️ Implementation needs human attention"
        if result.pr_number is not None and result.pr_url:
            summary = f"⚠️ PR #{result.pr_number} created — {result.pr_url}"
        if result.error:
            return f"{summary}\nNeeds human attention: {result.error}"
        return summary

    if result.status == "failed":
        detail = result.error or "see agent logs for details."
        return f"❌ Implementation failed — {detail}"

    if result.status == "rejected":
        detail = result.error or "issue author is not trusted."
        return f"⚠️ Implementation rejected — {detail}"

    emoji = STATUS_EMOJI.get(result.status, "❓")
    return f"{emoji} Implementation {result.status}"


async def _publish_implement_result(
    repo: str,
    issue_number: int,
    progress_comment_id: int | None,
    result: TaskResult,
    *,
    effort: str,
    allow_fallback_comment: bool = True,
) -> None:
    body = _format_implement_result_comment(result, effort=effort)
    if progress_comment_id is not None:
        await _update_progress_comment(repo, progress_comment_id, body)
        return
    if allow_fallback_comment and result.status != "rejected":
        await safe_comment(repo, issue_number, body)


async def _run_implement(repo: str, issue_number: int, model: str, effort: str) -> TaskResult:
    """Run the implement lifecycle and return a typed result."""
    progress_comment_id: int | None = None
    issue = None

    try:
        try:
            issue = await get_issue(repo, issue_number)
        except Exception:
            logger.warning(
                "Failed to fetch issue %s#%d before implementation",
                repo,
                issue_number,
                exc_info=True,
            )

        if issue is not None:
            progress_comment_id = await _start_progress_comment(
                repo,
                issue_number,
                body=f"{IMPLEMENT_PROGRESS_PREFIX}{issue_number}...",
                body_prefix=IMPLEMENT_PROGRESS_PREFIX,
            )

        result = await implement_issue(
            repo=repo,
            issue_number=issue_number,
            model=model,
            reasoning_effort=effort,
            issue=issue,
        )

        await _publish_implement_result(
            repo,
            issue_number,
            progress_comment_id,
            result,
            effort=effort,
        )
        return result

    except ValueError as exc:
        logger.warning("Implementation rejected for %s#%d: %s", repo, issue_number, exc)
        result = TaskResult(status="rejected", premium_requests=0, error=str(exc))
        await _publish_implement_result(
            repo,
            issue_number,
            progress_comment_id,
            result,
            effort=effort,
            allow_fallback_comment=False,
        )
        return result

    except TaskError as exc:
        logger.exception("Implementation failed for %s#%d", repo, issue_number)
        result = TaskResult(
            status="failed",
            premium_requests=exc.premium_requests,
            error=str(exc),
        )
        await _publish_implement_result(
            repo,
            issue_number,
            progress_comment_id,
            result,
            effort=effort,
            allow_fallback_comment=not exc.commented,
        )
        return result

    except Exception:
        logger.exception("Implementation failed for %s#%d", repo, issue_number)
        result = TaskResult(
            status="failed",
            premium_requests=0,
            error="see agent logs for details.",
        )
        await _publish_implement_result(
            repo,
            issue_number,
            progress_comment_id,
            result,
            effort=effort,
        )
        return result


async def _run_review(
    repo: str, pr_number: int, model: str, effort: str, session_id: str | None
) -> TaskResult:
    """Run the review lifecycle and return a typed result."""
    progress_comment_id: int | None = None

    try:
        progress_comment_id = await _start_progress_comment(
            repo,
            pr_number,
            body=f"{REVIEW_PROGRESS_PREFIX}{pr_number}...",
            body_prefix=REVIEW_PROGRESS_PREFIX,
        )

        result = await review_pr(
            repo=repo,
            pr_number=pr_number,
            model=model,
            reasoning_effort=effort,
            session_id=session_id,
        )

        await _update_progress_comment(
            repo, progress_comment_id, "✅ Review posted — see review above"
        )
        return result

    except TaskError as exc:
        logger.exception("Review failed for %s#%d", repo, pr_number)
        await _update_progress_comment(repo, progress_comment_id, f"⚠️ Review failed — {exc}")
        if not exc.commented:
            await safe_comment(repo, pr_number, f"⚠️ **Review failed** — {exc}")
        return TaskResult(status="failed", premium_requests=exc.premium_requests)

    except Exception as exc:
        logger.exception("Review failed for %s#%d", repo, pr_number)
        await _update_progress_comment(
            repo,
            progress_comment_id,
            "⚠️ Review failed — see agent logs for details.",
        )
        await safe_comment(repo, pr_number, "⚠️ **Review failed** — see agent logs for details.")
        return TaskResult(status="failed", premium_requests=0, error=str(exc))


async def main() -> int:
    """Worker entrypoint — dispatch to the appropriate task handler."""
    try:
        settings = WorkerSettings()  # ty: ignore[missing-argument]
    except Exception as exc:
        logger.error("Worker startup validation failed: %s", exc)
        return 1

    set_task_context(settings.task_type, settings.number)

    set_token(settings.gh_token)

    if settings.task_type == "implement":
        logger.info("Worker starting: implement %s#%d", settings.repo, settings.number)
        result = await _run_implement(
            settings.repo, settings.number, settings.model, settings.reasoning_effort
        )

    elif settings.task_type == "review":
        logger.info("Worker starting: review %s#%d", settings.repo, settings.number)
        result = await _run_review(
            settings.repo,
            settings.number,
            settings.model,
            settings.reasoning_effort,
            settings.session_id,
        )

    else:
        logger.error("Unknown task type: %s", settings.task_type)
        return 1

    # Write result as JSON to stdout for the API monitor to parse
    print(result.model_dump_json(exclude_unset=True), flush=True)

    status = result.status
    logger.info(
        "Worker finished: %s %s#%s → %s",
        settings.task_type,
        settings.repo,
        settings.number,
        status,
    )
    return 0 if status in ("complete", "partial") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
