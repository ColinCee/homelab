"""Agent service — FastAPI app for Beelink-hosted AI agents."""

import contextlib
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app
from pydantic import BaseModel

from copilot import TaskError
from git import reap_old_worktrees
from github import comment_on_issue
from implement import implement_issue
from metrics import (
    METRICS_REGISTRY,
    PREMIUM_REQUESTS_TOTAL,
    TASK_DURATION_SECONDS,
    TASK_IN_PROGRESS,
    TASK_TOTAL,
)
from review import review_pr

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
_implement_status: dict[str, dict] = {}


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
    model: str | None = None
    reasoning_effort: str | None = None


class ImplementRequest(BaseModel):
    repo: str
    issue_number: int
    model: str | None = None
    reasoning_effort: str | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# --- Review endpoints ---


@app.post("/review", status_code=202, response_model=None)
async def handle_review(req: ReviewRequest, background_tasks: BackgroundTasks):
    """Accept a review request and process it in the background."""
    model = req.model or MODEL
    effort = req.reasoning_effort or REASONING_EFFORT
    key = _review_key(req.repo, req.pr_number)

    existing = _review_status.get(key)
    if existing and existing["status"] == "in_progress":
        return JSONResponse(
            status_code=409,
            content={"status": "already_in_progress", "pr_number": req.pr_number},
        )

    _review_status[key] = {"status": "in_progress", "repo": req.repo, "pr_number": req.pr_number}

    background_tasks.add_task(
        _run_review,
        repo=req.repo,
        pr_number=req.pr_number,
        model=model,
        reasoning_effort=effort,
    )

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


async def _run_review(*, repo: str, pr_number: int, model: str, reasoning_effort: str) -> None:
    key = _review_key(repo, pr_number)
    status = "failed"
    premium_requests = 0
    start = time.monotonic()
    TASK_IN_PROGRESS.labels(task_type="review").inc()
    try:
        result = await review_pr(
            repo=repo,
            pr_number=pr_number,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        status = _task_status_label(result.get("status"))
        premium_requests = _premium_requests(result)
        _review_status[key] = {
            "status": result.get("status", "complete"),
            "repo": repo,
            "pr_number": pr_number,
            **result,
        }
    except TaskError as exc:
        logger.exception("Review failed for %s#%d", repo, pr_number)
        _review_status[key] = {"status": "failed", "repo": repo, "pr_number": pr_number}
        premium_requests = exc.premium_requests
        if not exc.commented:
            with contextlib.suppress(Exception):
                await comment_on_issue(repo, pr_number, f"⚠️ **Review failed** — {exc}")
    except Exception:
        logger.exception("Review failed for %s#%d", repo, pr_number)
        _review_status[key] = {"status": "failed", "repo": repo, "pr_number": pr_number}
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


async def _run_implement(
    *, repo: str, issue_number: int, model: str, reasoning_effort: str
) -> None:
    key = _implement_key(repo, issue_number)
    status = "failed"
    premium_requests = 0
    start = time.monotonic()
    TASK_IN_PROGRESS.labels(task_type="implement").inc()
    try:
        result = await implement_issue(
            repo=repo,
            issue_number=issue_number,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        status = _task_status_label(result.get("status"))
        premium_requests = _premium_requests(result)
        _implement_status[key] = {
            "status": result.get("status", "complete"),
            "repo": repo,
            "issue_number": issue_number,
            **result,
        }
    except ValueError as exc:
        # Trust boundary rejection (untrusted author) — don't interact with the issue
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
