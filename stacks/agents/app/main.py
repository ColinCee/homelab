"""Agent service — FastAPI app for Beelink-hosted AI agents."""

import logging
import os

from fastapi import BackgroundTasks, FastAPI
from pydantic import BaseModel

from review import review_pr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Homelab Agent Service", version="0.4.0")

MODEL = os.environ.get("MODEL", "gpt-5.4")
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "high")

# In-memory review status tracking
_review_status: dict[int, dict] = {}


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

    Returns 202 immediately. The agent fetches the PR, runs Copilot CLI
    with full codebase context, and posts the review directly to GitHub.
    """
    model = req.model or MODEL
    effort = req.reasoning_effort or REASONING_EFFORT

    _review_status[req.pr_number] = {"status": "in_progress", "pr_number": req.pr_number}

    background_tasks.add_task(
        _run_review,
        repo=req.repo,
        pr_number=req.pr_number,
        model=model,
        reasoning_effort=effort,
    )

    return {"status": "accepted", "pr_number": req.pr_number}


@app.get("/review/{pr_number}")
async def get_review_status(pr_number: int) -> dict:
    """Check the status of a review."""
    return _review_status.get(pr_number, {"status": "not_found", "pr_number": pr_number})


async def _run_review(*, repo: str, pr_number: int, model: str, reasoning_effort: str) -> None:
    """Background task: run the full review pipeline."""
    try:
        result = await review_pr(
            repo=repo,
            pr_number=pr_number,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        _review_status[pr_number] = {
            "status": "complete",
            "pr_number": pr_number,
            "verdict": result.verdict.value,
            "comment_count": len(result.comments),
            "elapsed_seconds": result.metadata.get("elapsed_seconds"),
        }
    except Exception:
        logger.exception("Review failed for %s#%d", repo, pr_number)
        _review_status[pr_number] = {
            "status": "failed",
            "pr_number": pr_number,
        }
