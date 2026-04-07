"""Agent service — FastAPI app for Beelink-hosted AI agents."""

import logging
import os

from fastapi import BackgroundTasks, FastAPI
from pydantic import BaseModel

from review import review_pr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Homelab Agent Service", version="0.1.0")

MODEL = os.environ.get("MODEL", "gpt-5.4")
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "high")


class ReviewRequest(BaseModel):
    repo: str
    pr_number: int
    model: str | None = None
    reasoning_effort: str | None = None


class ReviewResponse(BaseModel):
    status: str
    message: str


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/review", response_model=ReviewResponse)
async def handle_review(req: ReviewRequest, background_tasks: BackgroundTasks) -> ReviewResponse:
    model = req.model or MODEL
    effort = req.reasoning_effort or REASONING_EFFORT

    background_tasks.add_task(
        review_pr,
        repo=req.repo,
        pr_number=req.pr_number,
        model=model,
        reasoning_effort=effort,
    )

    return ReviewResponse(
        status="accepted",
        message=f"Review queued for {req.repo}#{req.pr_number} with {model}",
    )
