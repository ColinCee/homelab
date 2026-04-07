"""Agent service — FastAPI app for Beelink-hosted AI agents."""

import logging
import os

from fastapi import FastAPI
from pydantic import BaseModel

from review import review_pr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Homelab Agent Service", version="0.3.0")

MODEL = os.environ.get("MODEL", "gpt-5.4")
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "high")


class ReviewRequest(BaseModel):
    repo: str
    pr_number: int
    model: str | None = None
    reasoning_effort: str | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/review")
async def handle_review(req: ReviewRequest) -> dict:
    """Run AI review and return structured result for GitHub Reviews API."""
    model = req.model or MODEL
    effort = req.reasoning_effort or REASONING_EFFORT

    result = await review_pr(
        repo=req.repo,
        pr_number=req.pr_number,
        model=model,
        reasoning_effort=effort,
    )

    return result.to_dict()
