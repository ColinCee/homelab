"""Agent service — FastAPI app for Beelink-hosted AI agents."""

import logging
import os

from fastapi import BackgroundTasks, FastAPI
from pydantic import BaseModel

from review import review_pr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Homelab Agent Service", version="0.5.0")

MODEL = os.environ.get("MODEL", "gpt-5.4")
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "high")

_review_status: dict[str, dict] = {}


def _review_key(repo: str, pr_number: int) -> str:
    return f"{repo}#{pr_number}"


class ReviewRequest(BaseModel):
    repo: str
    pr_number: int
    model: str | None = None
    reasoning_effort: str | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/review", status_code=202)
async def handle_review(req: ReviewRequest, background_tasks: BackgroundTasks) -> dict:
    """Accept a review request and process it in the background.

    Returns 202 immediately. The Copilot CLI agent reviews the PR and
    posts the review directly to GitHub via gh CLI.
    """
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

    # Fallback: search by pr_number for backward compatibility
    for v in _review_status.values():
        if v.get("pr_number") == pr_number:
            return v

    return {"status": "not_found", "pr_number": pr_number}


async def _run_review(*, repo: str, pr_number: int, model: str, reasoning_effort: str) -> None:
    """Background task: run the full review pipeline."""
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
        _review_status[key] = {
            "status": "failed",
            "repo": repo,
            "pr_number": pr_number,
        }
