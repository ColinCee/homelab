"""Agent service — FastAPI app for Beelink-hosted AI agents."""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth import require_bearer
from logging_config import configure_logging, resolve_log_format
from models import TaskResult
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

LOG_FORMAT = resolve_log_format(os.environ.get("LOG_FORMAT"))
configure_logging(LOG_FORMAT)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await reap_old_worktrees()

    # Clean up workers that completed while we were down. The worker usually
    # emitted its own task_completed event; emit an API fallback only if it did not.
    for w in await cleanup_orphaned_workers():
        logs = str(w.get("logs", ""))
        result = parse_worker_result(logs)
        fallback = "failed"
        status = _task_status_label(result.status if result else fallback)
        premium = _premium_requests(result)
        if not _logs_contain_task_completion(logs):
            _emit_task_completion(
                task_type=str(w["task_type"]),
                number=int(w["number"]),
                status=status,
                duration_seconds=float(w.get("duration_seconds", 0)),
                premium_requests=premium,
                result=result,
                source="api_orphan_harvest",
            )
        logger.info(
            "Cleaned up orphaned worker %s #%s (status=%s, premium=%d)",
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
        _spawn_monitor(container_id, task_type=task_type, number=number, start=approx_start)
        logger.info("Reconnected monitor for running worker %s #%d", task_type, number)

    yield


app = FastAPI(title="Homelab Agent Service", version="0.7.0", lifespan=lifespan)

MODEL = os.environ.get("MODEL", "gpt-5.4")
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "high")

# Tracks active monitor tasks so we can clean them up if needed
_monitor_tasks: dict[str, asyncio.Task[None]] = {}


def _task_status_label(status: object) -> str:
    if status in ("complete", "failed", "partial", "rejected"):
        return str(status)
    return "failed"


def _premium_requests(result: TaskResult | None) -> int:
    return result.premium_requests if result else 0


def _worker_log_format() -> str:
    return resolve_log_format(os.environ.get("LOG_FORMAT"))


def _logs_contain_task_completion(logs: str) -> bool:
    for line in logs.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("event") == "task_completed":
            return True
    return False


def _emit_task_completion(
    *,
    task_type: str,
    number: int,
    status: str,
    duration_seconds: float,
    premium_requests: int,
    result: TaskResult | None,
    source: str,
) -> None:
    event: dict[str, object] = {
        "event": "task_completed",
        "source": source,
        "task_type": task_type,
        "status": status,
        "duration_seconds": round(duration_seconds, 3),
        "premium_requests": premium_requests,
        "input_tokens": result.input_tokens if result else 0,
        "output_tokens": result.output_tokens if result else 0,
        "cached_tokens": result.cached_tokens if result else 0,
        "reasoning_tokens": result.reasoning_tokens if result else 0,
    }
    if task_type == "implement":
        event["issue_number"] = number
    elif task_type == "review":
        event["pr_number"] = number
    if result and result.repo:
        event["repo"] = result.repo
    if result and result.error:
        event["error"] = result.error

    logger.info("task_completed", extra=event)


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
    """Wait for a worker container to exit and clean it up."""
    monitor_key = f"{task_type}-{number}"
    try:
        exit_code = await wait_container(container_id)
        duration = time.monotonic() - start

        result: TaskResult | None = None
        logs = ""
        try:
            logs = await get_logs(container_id)
            result = parse_worker_result(logs)
        except Exception:
            logger.warning("Failed to parse worker result for %s #%d", task_type, number)

        if exit_code != 0:
            logger.warning(
                "Worker %s #%d output (last 3000 chars):\n%s",
                task_type,
                number,
                logs[-3000:],
            )

        fallback_status = "failed" if exit_code != 0 else "complete"
        status = _task_status_label(result.status if result else fallback_status)
        premium = _premium_requests(result)

        if not _logs_contain_task_completion(logs):
            _emit_task_completion(
                task_type=task_type,
                number=number,
                status=status,
                duration_seconds=duration,
                premium_requests=premium,
                result=result,
                source="api_monitor",
            )
        error = result.error if result and result.error else ""
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
        try:
            await remove_container(container_id)
        except Exception:
            logger.warning(
                "Failed to remove worker container %s for %s #%d",
                container_id,
                task_type,
                number,
                exc_info=True,
            )
        _monitor_tasks.pop(monitor_key, None)


def _spawn_monitor(container_id: str, *, task_type: str, number: int, start: float) -> None:
    """Start a background task to monitor a worker container."""
    monitor_key = f"{task_type}-{number}"
    task = asyncio.create_task(
        _monitor_worker(container_id, task_type=task_type, number=number, start=start)
    )
    _monitor_tasks[monitor_key] = task


async def _dispatch_worker(
    *,
    task_type: str,
    repo: str,
    number: int,
    github_token: str,
    model: str,
    effort: str,
) -> str:
    """Spawn a worker container and start its monitor. Returns the container ID."""
    image = await get_own_image()
    env = {
        "TASK_TYPE": task_type,
        "REPO": repo,
        "NUMBER": str(number),
        "GH_TOKEN": github_token,
        "COPILOT_GITHUB_TOKEN": os.environ.get("COPILOT_GITHUB_TOKEN", ""),
        "LOG_FORMAT": _worker_log_format(),
        "MODEL": model,
        "REASONING_EFFORT": effort,
    }

    start = time.monotonic()

    container_id = await spawn_worker(
        task_type=task_type,
        image=image,
        env=env,
        number=number,
        volumes=["repo-cache:/repo.git", "reviews:/reviews"],
    )

    _spawn_monitor(container_id, task_type=task_type, number=number, start=start)
    return container_id


# --- Review endpoints ---


@app.post("/review", status_code=202, response_model=None, dependencies=[Depends(require_bearer)])
async def handle_review(req: ReviewRequest):
    """Accept a review request and spawn a worker container."""
    if req.triggered_by not in ALLOWED_ACTORS:
        return JSONResponse(
            status_code=403,
            content={"error": f"Actor '{req.triggered_by}' is not allowed"},
        )
    set_token(req.github_token)

    await _dispatch_worker(
        task_type="review",
        repo=req.repo,
        number=req.pr_number,
        github_token=req.github_token,
        model=req.model or MODEL,
        effort=req.reasoning_effort or REASONING_EFFORT,
    )
    return {"status": "accepted", "pr_number": req.pr_number}


@app.get("/review/{pr_number}")
async def get_review_status(pr_number: int, repo: str = "") -> dict[str, str | int]:
    """Check the status of a review by checking the worker container."""
    running = await is_worker_running("review", pr_number)
    if running:
        return {"status": "in_progress", "pr_number": pr_number}
    return {"status": "not_found", "pr_number": pr_number}


# --- Implement endpoints ---


@app.post(
    "/implement", status_code=202, response_model=None, dependencies=[Depends(require_bearer)]
)
async def handle_implement(req: ImplementRequest):
    """Accept an implementation request and spawn a worker container."""
    if req.triggered_by not in ALLOWED_ACTORS:
        return JSONResponse(
            status_code=403,
            content={"error": f"Actor '{req.triggered_by}' is not allowed"},
        )
    set_token(req.github_token)

    if await is_worker_running("implement", req.issue_number):
        return JSONResponse(
            status_code=409,
            content={"status": "already_in_progress", "issue_number": req.issue_number},
        )

    await _dispatch_worker(
        task_type="implement",
        repo=req.repo,
        number=req.issue_number,
        github_token=req.github_token,
        model=req.model or MODEL,
        effort=req.reasoning_effort or REASONING_EFFORT,
    )
    return {"status": "accepted", "issue_number": req.issue_number}


@app.get("/implement/{issue_number}")
async def get_implement_status(issue_number: int, repo: str = "") -> dict[str, str | int]:
    """Check the status of an implementation by checking the worker container."""
    running = await is_worker_running("implement", issue_number)
    if running:
        return {"status": "in_progress", "issue_number": issue_number}
    return {"status": "not_found", "issue_number": issue_number}
