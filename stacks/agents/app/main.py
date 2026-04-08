"""Agent service — FastAPI app for Beelink-hosted AI agents."""

import logging
import os

from fastapi import BackgroundTasks, FastAPI
from pydantic import BaseModel

from implement import fix_pr, implement_issue
from review import review_pr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Homelab Agent Service", version="0.6.0")

MODEL = os.environ.get("MODEL", "gpt-5.4")
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "high")

_review_status: dict[str, dict] = {}
_implement_status: dict[str, dict] = {}


def _review_key(repo: str, pr_number: int) -> str:
    return f"{repo}#{pr_number}"


def _implement_key(repo: str, issue_number: int) -> str:
    return f"{repo}#{issue_number}"


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


class FixRequest(BaseModel):
    repo: str
    pr_number: int
    model: str | None = None
    reasoning_effort: str | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# --- Review endpoints ---


@app.post("/review", status_code=202)
async def handle_review(req: ReviewRequest, background_tasks: BackgroundTasks) -> dict:
    """Accept a review request and process it in the background."""
    model = req.model or MODEL
    effort = req.reasoning_effort or REASONING_EFFORT
    key = _review_key(req.repo, req.pr_number)

    existing = _review_status.get(key)
    if existing and existing["status"] == "in_progress":
        return {"status": "already_in_progress", "pr_number": req.pr_number}

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


@app.post("/implement", status_code=202)
async def handle_implement(req: ImplementRequest, background_tasks: BackgroundTasks) -> dict:
    """Accept an implementation request and process it in the background."""
    model = req.model or MODEL
    effort = req.reasoning_effort or REASONING_EFFORT
    key = _implement_key(req.repo, req.issue_number)

    existing = _implement_status.get(key)
    if existing and existing["status"] == "in_progress":
        return {"status": "already_in_progress", "issue_number": req.issue_number}

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


# --- Fix endpoint ---


@app.post("/fix", status_code=202)
async def handle_fix(req: FixRequest, background_tasks: BackgroundTasks) -> dict:
    """Accept a fix request to address review feedback on a PR."""
    model = req.model or MODEL
    effort = req.reasoning_effort or REASONING_EFFORT
    key = _review_key(req.repo, req.pr_number)

    existing = _review_status.get(key)
    if existing and existing["status"] == "in_progress":
        return {"status": "already_in_progress", "pr_number": req.pr_number}

    _review_status[key] = {"status": "in_progress", "repo": req.repo, "pr_number": req.pr_number}

    background_tasks.add_task(
        _run_fix,
        repo=req.repo,
        pr_number=req.pr_number,
        model=model,
        reasoning_effort=effort,
    )

    return {"status": "accepted", "pr_number": req.pr_number}


# --- Background tasks ---


async def _run_review(*, repo: str, pr_number: int, model: str, reasoning_effort: str) -> None:
    key = _review_key(repo, pr_number)
    try:
        result = await review_pr(
            repo=repo,
            pr_number=pr_number,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        _review_status[key] = {
            "status": "complete",
            "repo": repo,
            "pr_number": pr_number,
            "elapsed_seconds": result.get("elapsed_seconds"),
        }
    except Exception:
        logger.exception("Review failed for %s#%d", repo, pr_number)
        _review_status[key] = {"status": "failed", "repo": repo, "pr_number": pr_number}


async def _run_implement(
    *, repo: str, issue_number: int, model: str, reasoning_effort: str
) -> None:
    key = _implement_key(repo, issue_number)
    try:
        result = await implement_issue(
            repo=repo,
            issue_number=issue_number,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        _implement_status[key] = {
            "status": "complete",
            "repo": repo,
            "issue_number": issue_number,
            "pr_number": result.get("pr_number"),
            "pr_url": result.get("pr_url"),
            "elapsed_seconds": result.get("elapsed_seconds"),
        }
    except Exception:
        logger.exception("Implementation failed for %s#%d", repo, issue_number)
        _implement_status[key] = {
            "status": "failed",
            "repo": repo,
            "issue_number": issue_number,
        }


async def _run_fix(*, repo: str, pr_number: int, model: str, reasoning_effort: str) -> None:
    key = _review_key(repo, pr_number)
    try:
        result = await fix_pr(
            repo=repo,
            pr_number=pr_number,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        _review_status[key] = {
            "status": "complete",
            "repo": repo,
            "pr_number": pr_number,
            "elapsed_seconds": result.get("elapsed_seconds"),
        }
    except Exception:
        logger.exception("Fix failed for %s#%d", repo, pr_number)
        _review_status[key] = {"status": "failed", "repo": repo, "pr_number": pr_number}
