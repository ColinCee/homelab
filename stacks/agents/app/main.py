"""Agent service — FastAPI app for Beelink-hosted AI agents."""

import asyncio
import contextlib
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app
from pydantic import BaseModel

from metrics import (
    METRICS_REGISTRY,
    PREMIUM_REQUESTS_TOTAL,
    TASK_DURATION_SECONDS,
    TASK_IN_PROGRESS,
    TASK_TOTAL,
)
from services.docker import (
    cleanup_orphaned_workers,
    discover_running_workers,
    get_logs,
    get_own_image,
    is_worker_running,
    parse_worker_result,
    remove_container,
    spawn_worker,
    wait_container,
)
from services.git import reap_old_worktrees
from services.github import set_token
from trust import ALLOWED_ACTORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await reap_old_worktrees()

    # Harvest metrics from workers that completed while we were down
    for w in await cleanup_orphaned_workers():
        result = parse_worker_result(str(w.get("logs", "")))
        fallback = "failed"
        status = _task_status_label(result.get("status", fallback))
        premium = _premium_requests(result)
        _record_task_metrics(
            task_type=str(w["task_type"]),
            status=status,
            duration_seconds=float(w.get("duration_seconds", 0)),
            premium_requests=premium,
        )
        logger.info(
            "Harvested metrics from orphaned worker %s #%s (status=%s, premium=%d)",
            w["task_type"],
            w["number"],
            status,
            premium,
        )

    # Reconnect monitors for workers still running from a previous API process
    for w in await discover_running_workers():
        task_type = str(w["task_type"])
        number = int(w["number"])
        container_id = str(w["container_id"])
        started_at = float(w.get("started_at", 0))
        elapsed = time.time() - started_at if started_at > 0 else 0
        approx_start = time.monotonic() - elapsed
        TASK_IN_PROGRESS.labels(task_type=task_type).inc()
        _spawn_monitor(container_id, task_type=task_type, number=number, start=approx_start)
        logger.info("Reconnected monitor for running worker %s #%d", task_type, number)

    yield


app = FastAPI(title="Homelab Agent Service", version="0.7.0", lifespan=lifespan)
app.mount("/metrics", make_asgi_app(registry=METRICS_REGISTRY))

MODEL = os.environ.get("MODEL", "gpt-5.4")
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "high")

# Tracks active monitor tasks so we can clean them up if needed
_monitor_tasks: dict[str, asyncio.Task[None]] = {}


def _task_status_label(status: object) -> str:
    if status in ("complete", "failed", "partial", "rejected"):
        return str(status)
    return "failed"


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


# --- Worker monitoring ---


async def _monitor_worker(container_id: str, *, task_type: str, number: int, start: float) -> None:
    """Wait for a worker container to exit and record metrics."""
    monitor_key = f"{task_type}-{number}"
    try:
        exit_code = await wait_container(container_id)
        duration = time.monotonic() - start

        result: dict = {}
        try:
            logs = await get_logs(container_id)
            logger.info("Worker %s #%d raw output:\n%s", task_type, number, logs[-3000:])
            result = parse_worker_result(logs)
        except Exception:
            logger.warning("Failed to parse worker result for %s #%d", task_type, number)

        fallback_status = "failed" if exit_code != 0 else "complete"
        status = _task_status_label(result.get("status", fallback_status))
        premium = _premium_requests(result)

        _record_task_metrics(
            task_type=task_type,
            status=status,
            duration_seconds=duration,
            premium_requests=premium,
        )
        error = result.get("error", "")
        logger.info(
            "Worker %s #%d finished (exit=%d, status=%s, duration=%.0fs, premium=%d%s)",
            task_type,
            number,
            exit_code,
            status,
            duration,
            premium,
            f", error={error}" if error else "",
        )
    except Exception:
        logger.exception("Monitor failed for worker %s #%d", task_type, number)
    finally:
        TASK_IN_PROGRESS.labels(task_type=task_type).dec()
        with contextlib.suppress(Exception):
            await remove_container(container_id)
        _monitor_tasks.pop(monitor_key, None)


def _spawn_monitor(container_id: str, *, task_type: str, number: int, start: float) -> None:
    """Start a background task to monitor a worker container."""
    monitor_key = f"{task_type}-{number}"
    task = asyncio.create_task(
        _monitor_worker(container_id, task_type=task_type, number=number, start=start)
    )
    _monitor_tasks[monitor_key] = task


# --- Review endpoints ---


@app.post("/review", status_code=202, response_model=None)
async def handle_review(req: ReviewRequest):
    """Accept a review request and spawn a worker container."""
    if req.triggered_by not in ALLOWED_ACTORS:
        return JSONResponse(
            status_code=403,
            content={"error": f"Actor '{req.triggered_by}' is not allowed"},
        )
    set_token(req.github_token)
    model = req.model or MODEL
    effort = req.reasoning_effort or REASONING_EFFORT

    image = await get_own_image()
    env = {
        "TASK_TYPE": "review",
        "REPO": req.repo,
        "NUMBER": str(req.pr_number),
        "GH_TOKEN": req.github_token,
        "COPILOT_GITHUB_TOKEN": os.environ.get("COPILOT_GITHUB_TOKEN", ""),
        "MODEL": model,
        "REASONING_EFFORT": effort,
    }

    start = time.monotonic()
    TASK_IN_PROGRESS.labels(task_type="review").inc()

    container_id = await spawn_worker(
        task_type="review",
        image=image,
        env=env,
        number=req.pr_number,
        volumes=["repo-cache:/repo.git", "reviews:/reviews"],
    )

    _spawn_monitor(container_id, task_type="review", number=req.pr_number, start=start)
    return {"status": "accepted", "pr_number": req.pr_number}


@app.get("/review/{pr_number}")
async def get_review_status(pr_number: int, repo: str = "") -> dict[str, str | int]:
    """Check the status of a review by checking the worker container."""
    running = await is_worker_running("review", pr_number)
    if running:
        return {"status": "in_progress", "pr_number": pr_number}
    return {"status": "not_found", "pr_number": pr_number}


# --- Implement endpoints ---


@app.post("/implement", status_code=202, response_model=None)
async def handle_implement(req: ImplementRequest):
    """Accept an implementation request and spawn a worker container."""
    if req.triggered_by not in ALLOWED_ACTORS:
        return JSONResponse(
            status_code=403,
            content={"error": f"Actor '{req.triggered_by}' is not allowed"},
        )
    set_token(req.github_token)
    model = req.model or MODEL
    effort = req.reasoning_effort or REASONING_EFFORT

    # Check for duplicate via running container
    if await is_worker_running("implement", req.issue_number):
        return JSONResponse(
            status_code=409,
            content={"status": "already_in_progress", "issue_number": req.issue_number},
        )

    image = await get_own_image()
    env = {
        "TASK_TYPE": "implement",
        "REPO": req.repo,
        "NUMBER": str(req.issue_number),
        "GH_TOKEN": req.github_token,
        "COPILOT_GITHUB_TOKEN": os.environ.get("COPILOT_GITHUB_TOKEN", ""),
        "MODEL": model,
        "REASONING_EFFORT": effort,
    }

    start = time.monotonic()
    TASK_IN_PROGRESS.labels(task_type="implement").inc()

    container_id = await spawn_worker(
        task_type="implement",
        image=image,
        env=env,
        number=req.issue_number,
        volumes=["repo-cache:/repo.git", "reviews:/reviews"],
    )

    _spawn_monitor(container_id, task_type="implement", number=req.issue_number, start=start)
    return {"status": "accepted", "issue_number": req.issue_number}


@app.get("/implement/{issue_number}")
async def get_implement_status(issue_number: int, repo: str = "") -> dict[str, str | int]:
    """Check the status of an implementation by checking the worker container."""
    running = await is_worker_running("implement", issue_number)
    if running:
        return {"status": "in_progress", "issue_number": issue_number}
    return {"status": "not_found", "issue_number": issue_number}
