from __future__ import annotations

import logging
import os
import time

import httpx

from .models import EMBEDDING_DIMENSION

logger = logging.getLogger(__name__)

GITHUB_MODELS_URL = (
    "https://models.github.ai/inference/openai/deployments/text-embedding-3-large/embeddings"
)
MODEL_NAME = "text-embedding-3-large"
TOKEN_ENV = "GITHUB_TOKEN"

_MAX_RETRIES = 3
_INITIAL_BACKOFF = 1.0


def get_embeddings(texts: list[str], *, token: str | None = None) -> list[list[float]]:
    """Call GitHub Models API to embed a batch of texts. Returns one vector per input."""
    if not texts:
        return []

    resolved_token = token or os.getenv(TOKEN_ENV)
    if not resolved_token:
        raise RuntimeError(f"{TOKEN_ENV} must be set for embedding requests")

    payload = {"input": texts, "model": MODEL_NAME}
    headers = {"Authorization": f"Bearer {resolved_token}", "Content-Type": "application/json"}

    data = _post_with_retry(payload, headers)
    embeddings = _parse_response(data, expected=len(texts))
    return embeddings


def _post_with_retry(payload: dict, headers: dict) -> dict:
    """POST with exponential backoff on 429 / 5xx."""
    backoff = _INITIAL_BACKOFF
    last_error: httpx.HTTPStatusError | None = None

    for attempt in range(_MAX_RETRIES):
        response = httpx.post(GITHUB_MODELS_URL, json=payload, headers=headers, timeout=60.0)
        try:
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code == 429 or exc.response.status_code >= 500:
                logger.warning(
                    "Embedding API returned %d (attempt %d/%d), retrying in %.1fs",
                    exc.response.status_code,
                    attempt + 1,
                    _MAX_RETRIES,
                    backoff,
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            raise

    if last_error is not None:
        raise last_error
    raise RuntimeError("No retries attempted")


def _parse_response(data: dict, *, expected: int) -> list[list[float]]:
    """Extract and validate embedding vectors from the API response."""
    items = data.get("data", [])
    if len(items) != expected:
        raise ValueError(f"Expected {expected} embeddings, got {len(items)}")

    sorted_items = sorted(items, key=lambda item: item["index"])
    embeddings: list[list[float]] = []
    for item in sorted_items:
        vec = item["embedding"]
        if len(vec) != EMBEDDING_DIMENSION:
            raise ValueError(f"Expected {EMBEDDING_DIMENSION}-dim embedding, got {len(vec)}")
        embeddings.append(vec)

    return embeddings
