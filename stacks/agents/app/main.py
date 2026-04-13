"""Agent service — FastAPI app for Beelink-hosted AI agents."""

import asyncio
import contextlib
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app
from pydantic import BaseModel

from implement import implement_issue
from metrics import (
    METRICS_REGISTRY,
    PREMIUM_REQUESTS_TOTAL,
    TASK_DURATION_SECONDS,
    TASK_IN_PROGRESS,
    TASK_TOTAL,
)
from review import review_pr
from services.copilot import TaskError
from services.git import reap_old_worktrees
from services.github import (
    comment_on_issue,
    find_issue_comment_by_body_prefix,
    get_issue,
    set_token,
    update_comment,
)
from trust import ALLOWED_ACTORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await reap_old_worktrees()
    yield


app = FastAPI(title="Homelab Agent Service", version="0.6.0", lifespan=lifespan)
app.mount("/metrics", make_asgi_app(registry=METRICS_REGISTRY))

MODEL = os.environ.get("MODEL", "gpt-5.4")
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "high")

_review_status: dict[str, dict] = {}
_review_tasks: dict[str, asyncio.Task[None]] = {}
_review_locks: dict[str, asyncio.Lock] = {}
_review_request_ids: dict[str, int] = {}
_implement_status: dict[str, dict] = {}

REVIEW_PROGRESS_PREFIX = "🔄 Review in progress for PR #"
IMPLEMENT_PROGRESS_PREFIX = "🔄 Implementing #"


def _review_key(repo: str, pr_number: int) -> str:
    return f"{repo}#{pr_number}"


def _implement_key(repo: str, issue_number: int) -> str:
    return f"{repo}#{issue_number}"


def _task_status_label(status: object) -> str:
    if status == "failed":
        return "failed"
    if status == "partial":
        return "partial"
    return "complete"


def _premium_requests(result: dict[str, object] | None) -> int:
    if not result:
        return 0

    premium_requests = result.get("premium_requests", 0)
    return premium_requests if isinstance(premium_requests, int) else 0


def _review_progress_comment(pr_number: int) -> str:
    return f"{REVIEW_PROGRESS_PREFIX}{pr_number}..."


def _review_progress_failure_comment(reason: str) -> str:
    return f"⚠️ Review failed — {reason}"


def _review_progress_cancelled_comment() -> str:
    return "⏹️ Review cancelled — superseded by newer /review request"


def _implement_progress_comment(issue_number: int) -> str:
    return f"{IMPLEMENT_PROGRESS_PREFIX}{issue_number}..."


def _implement_progress_success_comment(
    pr_number: int, pr_url: str, auto_merge: bool = False
) -> str:
    if auto_merge:
        return f"✅ PR #{pr_number} created (auto-merge enabled) — {pr_url}"
    return f"✅ PR #{pr_number} created — {pr_url}"


def _implement_progress_failure_comment(reason: str) -> str:
    return f"⚠️ Implementation failed — {reason}"


async def _update_progress_comment(repo: str, comment_id: int | None, body: str) -> None:
    if comment_id is None:
        return

    with contextlib.suppress(Exception):
        await update_comment(repo, comment_id, body)


async def _start_progress_comment(
    repo: str, issue_number: int, *, body: str, body_prefix: str
) -> int | None:
    with contextlib.suppress(Exception):
        comment_id = await find_issue_comment_by_body_prefix(repo, issue_number, body_prefix)
        if comment_id is not None:
            await update_comment(repo, comment_id, body)
            return comment_id
        return await comment_on_issue(repo, issue_number, body)
    return None


async def _start_review_progress_comment(repo: str, pr_number: int) -> int | None:
    return await _start_progress_comment(
        repo,
        pr_number,
        body=_review_progress_comment(pr_number),
        body_prefix=REVIEW_PROGRESS_PREFIX,
    )


async def _start_implement_progress_comment(repo: str, issue_number: int) -> int | None:
    return await _start_progress_comment(
        repo,
        issue_number,
        body=_implement_progress_comment(issue_number),
        body_prefix=IMPLEMENT_PROGRESS_PREFIX,
    )


async def _get_issue_for_progress(repo: str, issue_number: int) -> dict | None:
    try:
        return await get_issue(repo, issue_number)
    except Exception:
        return None


def _record_task_metrics(
    *, task_type: str, status: str, duration_seconds: float, premium_requests: int
) -> None:
    TASK_DURATION_SECONDS.labels(task_type=task_type, status=status).observe(duration_seconds)
    TASK_TOTAL.labels(task_type=task_type, status=status).inc()
    if premium_requests:
        PREMIUM_REQUESTS_TOTAL.labels(task_type=task_type).inc(premium_requests)


class ReviewRequest(BaseModel):
    repo: str
    pr_number: int
    triggered_by: str
    github_token: str
    model: str | None = None
    reasoning_effort: str | None = None


class ImplementRequest(BaseModel):
    repo: str
    issue_number: int
    triggered_by: str
    github_token: str
    model: str | None = None
    reasoning_effort: str | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# --- Review endpoints ---


@app.post("/review", status_code=202, response_model=None)
async def handle_review(req: ReviewRequest):
    """Accept a review request and process it in the background."""
    if req.triggered_by not in ALLOWED_ACTORS:
        return JSONResponse(
            status_code=403,
            content={"error": f"Actor '{req.triggered_by}' is not allowed"},
        )
    set_token(req.github_token)
    model = req.model or MODEL
    effort = req.reasoning_effort or REASONING_EFFORT
    key = _review_key(req.repo, req.pr_number)
    lock = _review_locks.setdefault(key, asyncio.Lock())
    existing_task: asyncio.Task[None] | None = None

    async with lock:
        request_id = _review_request_ids.get(key, 0) + 1
        _review_request_ids[key] = request_id
        existing_task = _review_tasks.get(key)
        if existing_task and not existing_task.done():
            existing_task.cancel()

    if existing_task and not existing_task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await existing_task

    async with lock:
        if _review_request_ids.get(key) != request_id:
            return {"status": "accepted", "pr_number": req.pr_number}

        existing = _review_status.get(key)
        prior_session_id = (
            existing.get("session_id")
            if existing and existing.get("status") not in {"in_progress", "cancelled"}
            else None
        )

        _review_status[key] = {
            "status": "in_progress",
            "repo": req.repo,
            "pr_number": req.pr_number,
        }
        task = asyncio.create_task(
            _run_review(
                repo=req.repo,
                pr_number=req.pr_number,
                model=model,
                reasoning_effort=effort,
                session_id=prior_session_id,
            )
        )
        _review_tasks[key] = task

    return {"status": "accepted", "pr_number": req.pr_number}


@app.get("/review/{pr_number}")
async def get_review_status(pr_number: int, repo: str = "") -> dict:
    """Check the status of a review."""
    key = _review_key(repo, pr_number) if repo else None

    if key and key in _review_status:
        return _review_status[key]

    for v in _review_status.values():
        if v.get("pr_number") == pr_number:
            return v

    return {"status": "not_found", "pr_number": pr_number}


# --- Implement endpoints ---


@app.post("/implement", status_code=202, response_model=None)
async def handle_implement(req: ImplementRequest, background_tasks: BackgroundTasks):
    """Accept an implementation request and process it in the background."""
    if req.triggered_by not in ALLOWED_ACTORS:
        return JSONResponse(
            status_code=403,
            content={"error": f"Actor '{req.triggered_by}' is not allowed"},
        )
    set_token(req.github_token)
    model = req.model or MODEL
    effort = req.reasoning_effort or REASONING_EFFORT
    key = _implement_key(req.repo, req.issue_number)

    existing = _implement_status.get(key)
    if existing and existing["status"] == "in_progress":
        return JSONResponse(
            status_code=409,
            content={"status": "already_in_progress", "issue_number": req.issue_number},
        )

    _implement_status[key] = {
        "status": "in_progress",
        "repo": req.repo,
        "issue_number": req.issue_number,
    }

    background_tasks.add_task(
        _run_implement,
        repo=req.repo,
        issue_number=req.issue_number,
        model=model,
        reasoning_effort=effort,
    )

    return {"status": "accepted", "issue_number": req.issue_number}


@app.get("/implement/{issue_number}")
async def get_implement_status(issue_number: int, repo: str = "") -> dict:
    """Check the status of an implementation."""
    key = _implement_key(repo, issue_number) if repo else None

    if key and key in _implement_status:
        return _implement_status[key]

    for v in _implement_status.values():
        if v.get("issue_number") == issue_number:
            return v

    return {"status": "not_found", "issue_number": issue_number}


# --- Background tasks ---


async def _run_review(
    *, repo: str, pr_number: int, model: str, reasoning_effort: str, session_id: str | None = None
) -> None:
    key = _review_key(repo, pr_number)
    status = "failed"
    premium_requests = 0
    start = time.monotonic()
    progress_comment_id: int | None = None
    TASK_IN_PROGRESS.labels(task_type="review").inc()
    try:
        progress_comment_id = await _start_review_progress_comment(repo, pr_number)
        result = await review_pr(
            repo=repo,
            pr_number=pr_number,
            model=model,
            reasoning_effort=reasoning_effort,
            session_id=session_id,
        )
        status = _task_status_label(result.get("status"))
        premium_requests = _premium_requests(result)
        _review_status[key] = {
            "status": result.get("status", "complete"),
            "repo": repo,
            "pr_number": pr_number,
            **result,
        }
        await _update_progress_comment(
            repo, progress_comment_id, "✅ Review posted — see review above"
        )
    except asyncio.CancelledError:
        logger.info("Review cancelled for %s#%d", repo, pr_number)
        status = "cancelled"
        _review_status[key] = {"status": "cancelled", "repo": repo, "pr_number": pr_number}
        await _update_progress_comment(
            repo, progress_comment_id, _review_progress_cancelled_comment()
        )
        raise
    except TaskError as exc:
        logger.exception("Review failed for %s#%d", repo, pr_number)
        _review_status[key] = {"status": "failed", "repo": repo, "pr_number": pr_number}
        premium_requests = exc.premium_requests
        await _update_progress_comment(
            repo, progress_comment_id, _review_progress_failure_comment(str(exc))
        )
        if not exc.commented:
            with contextlib.suppress(Exception):
                await comment_on_issue(repo, pr_number, f"⚠️ **Review failed** — {exc}")
    except Exception:
        logger.exception("Review failed for %s#%d", repo, pr_number)
        _review_status[key] = {"status": "failed", "repo": repo, "pr_number": pr_number}
        await _update_progress_comment(
            repo,
            progress_comment_id,
            _review_progress_failure_comment("see agent logs for details."),
        )
        with contextlib.suppress(Exception):
            await comment_on_issue(
                repo, pr_number, "⚠️ **Review failed** — see agent logs for details."
            )
    finally:
        _record_task_metrics(
            task_type="review",
            status=status,
            duration_seconds=time.monotonic() - start,
            premium_requests=premium_requests,
        )
        TASK_IN_PROGRESS.labels(task_type="review").dec()
        current_task = asyncio.current_task()
        if current_task is not None and _review_tasks.get(key) is current_task:
            _review_tasks.pop(key, None)


async def _run_implement(
    *, repo: str, issue_number: int, model: str, reasoning_effort: str
) -> None:
    key = _implement_key(repo, issue_number)
    status = "failed"
    premium_requests = 0
    progress_comment_id: int | None = None
    start = time.monotonic()
    TASK_IN_PROGRESS.labels(task_type="implement").inc()
    try:
        issue = await _get_issue_for_progress(repo, issue_number)
        if issue is not None:
            progress_comment_id = await _start_implement_progress_comment(repo, issue_number)

        result = await implement_issue(
            repo=repo,
            issue_number=issue_number,
            model=model,
            reasoning_effort=reasoning_effort,
            issue=issue,
        )
        status = _task_status_label(result.get("status"))
        premium_requests = _premium_requests(result)
        _implement_status[key] = {
            "status": result.get("status", "complete"),
            "repo": repo,
            "issue_number": issue_number,
            **result,
        }
        pr_number = result.get("pr_number")
        pr_url = result.get("pr_url")
        auto_merge = result.get("auto_merge", False)
        if isinstance(pr_number, int) and isinstance(pr_url, str):
            await _update_progress_comment(
                repo,
                progress_comment_id,
                _implement_progress_success_comment(pr_number, pr_url, auto_merge=auto_merge),
            )
    except ValueError as exc:
        # Content trust rejection — don't interact with the issue
        logger.warning("Implementation rejected for %s#%d: %s", repo, issue_number, exc)
        _implement_status[key] = {
            "status": "rejected",
            "repo": repo,
            "issue_number": issue_number,
        }
    except TaskError as exc:
        logger.exception("Implementation failed for %s#%d", repo, issue_number)
        _implement_status[key] = {
            "status": "failed",
            "repo": repo,
            "issue_number": issue_number,
        }
        premium_requests = exc.premium_requests
        await _update_progress_comment(
            repo, progress_comment_id, _implement_progress_failure_comment(str(exc))
        )
        if not exc.commented:
            with contextlib.suppress(Exception):
                await comment_on_issue(
                    repo,
                    issue_number,
                    f"⚠️ **Implementation failed** — {exc}",
                )
    except Exception:
        logger.exception("Implementation failed for %s#%d", repo, issue_number)
        _implement_status[key] = {
            "status": "failed",
            "repo": repo,
            "issue_number": issue_number,
        }
        await _update_progress_comment(
            repo,
            progress_comment_id,
            _implement_progress_failure_comment("see agent logs for details."),
        )
        with contextlib.suppress(Exception):
            await comment_on_issue(
                repo,
                issue_number,
                "⚠️ **Implementation failed** — see agent logs for details.",
            )
    finally:
        _record_task_metrics(
            task_type="implement",
            status=status,
            duration_seconds=time.monotonic() - start,
            premium_requests=premium_requests,
        )
        TASK_IN_PROGRESS.labels(task_type="implement").dec()
