"""Ephemeral worker entrypoint — runs a single agent task then exits.

Spawned by the API via `docker run ... python -m worker`. Reads task
parameters from environment variables, calls the appropriate orchestrator,
posts progress/result comments to GitHub, and writes a JSON result to stdout
for the API's monitor coroutine to parse.
"""

import asyncio
import json
import logging
import sys

from implement import implement_issue
from logging_config import configure_logging
from review import review_pr
from runtime_env import WorkerSettings
from services.copilot import TaskError
from services.github import (
    comment_on_issue,
    find_issue_comment_by_body_prefix,
    get_issue,
    set_token,
    update_comment,
)

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


async def _run_implement(repo: str, issue_number: int, model: str, effort: str) -> dict:
    """Run the implement lifecycle and return a result dict."""
    progress_comment_id: int | None = None

    try:
        issue = None
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

        pr_number = result.get("pr_number")
        pr_url = result.get("pr_url")
        auto_merge = result.get("auto_merge", False)
        if isinstance(pr_number, int) and isinstance(pr_url, str):
            if auto_merge:
                msg = f"✅ PR #{pr_number} created (auto-merge enabled) — {pr_url}"
            else:
                msg = f"✅ PR #{pr_number} created — {pr_url}"
            await _update_progress_comment(repo, progress_comment_id, msg)

        return result

    except ValueError as exc:
        logger.warning("Implementation rejected for %s#%d: %s", repo, issue_number, exc)
        return {"status": "rejected", "premium_requests": 0}

    except TaskError as exc:
        logger.exception("Implementation failed for %s#%d", repo, issue_number)
        await _update_progress_comment(
            repo, progress_comment_id, f"⚠️ Implementation failed — {exc}"
        )
        if not exc.commented:
            try:
                await comment_on_issue(repo, issue_number, f"⚠️ **Implementation failed** — {exc}")
            except Exception:
                logger.warning(
                    "Failed to post implementation failure comment on %s#%d",
                    repo,
                    issue_number,
                    exc_info=True,
                )
        return {"status": "failed", "premium_requests": exc.premium_requests}

    except Exception as exc:
        logger.exception("Implementation failed for %s#%d", repo, issue_number)
        await _update_progress_comment(
            repo,
            progress_comment_id,
            "⚠️ Implementation failed — see agent logs for details.",
        )
        try:
            await comment_on_issue(
                repo, issue_number, "⚠️ **Implementation failed** — see agent logs for details."
            )
        except Exception:
            logger.warning(
                "Failed to post implementation failure comment on %s#%d",
                repo,
                issue_number,
                exc_info=True,
            )
        return {"status": "failed", "premium_requests": 0, "error": str(exc)}


async def _run_review(
    repo: str, pr_number: int, model: str, effort: str, session_id: str | None
) -> dict:
    """Run the review lifecycle and return a result dict."""
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
            try:
                await comment_on_issue(repo, pr_number, f"⚠️ **Review failed** — {exc}")
            except Exception:
                logger.warning(
                    "Failed to post review failure comment on %s#%d",
                    repo,
                    pr_number,
                    exc_info=True,
                )
        return {"status": "failed", "premium_requests": exc.premium_requests}

    except Exception as exc:
        logger.exception("Review failed for %s#%d", repo, pr_number)
        await _update_progress_comment(
            repo,
            progress_comment_id,
            "⚠️ Review failed — see agent logs for details.",
        )
        try:
            await comment_on_issue(
                repo, pr_number, "⚠️ **Review failed** — see agent logs for details."
            )
        except Exception:
            logger.warning(
                "Failed to post review failure comment on %s#%d",
                repo,
                pr_number,
                exc_info=True,
            )
        return {"status": "failed", "premium_requests": 0, "error": str(exc)}


async def main() -> int:
    """Worker entrypoint — dispatch to the appropriate task handler."""
    try:
        settings = WorkerSettings()  # ty: ignore[missing-argument]
    except Exception as exc:
        logger.error("Worker startup validation failed: %s", exc)
        return 1

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
    print(json.dumps(result), flush=True)

    status = result.get("status", "unknown")
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
