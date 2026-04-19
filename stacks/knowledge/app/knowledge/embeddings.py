from __future__ import annotations

import logging
import os
import time

import httpx

from .models import EMBEDDING_DIMENSION

logger = logging.getLogger(__name__)

GITHUB_MODELS_URL = "https://models.github.ai/inference/embeddings"
MODEL_NAME = "openai/text-embedding-3-large"
TOKEN_ENV = "COPILOT_GITHUB_TOKEN"

_MAX_RETRIES = 6
_INITIAL_BACKOFF = 2.0
_MAX_BACKOFF = 60.0
_BATCH_SIZE = 20


def get_embeddings(texts: list[str], *, token: str | None = None) -> list[list[float]]:
    """Call GitHub Models API to embed a batch of texts. Returns one vector per input.

    Splits into batches of _BATCH_SIZE to stay within per-request token limits.
    """
    if not texts:
        return []

    resolved_token = token or os.getenv(TOKEN_ENV)
    if not resolved_token:
        raise RuntimeError(f"{TOKEN_ENV} must be set for embedding requests")

    headers = {"Authorization": f"Bearer {resolved_token}", "Content-Type": "application/json"}

    all_embeddings: list[list[float]] = []
    for batch in _batches(texts, _BATCH_SIZE):
        payload = {"input": batch, "model": MODEL_NAME}
        data = _post_with_retry(payload, headers)
        all_embeddings.extend(_parse_response(data, expected=len(batch)))

    return all_embeddings


def _batches(items: list[str], size: int) -> list[list[str]]:
    """Split a list into consecutive batches of at most *size* items."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _post_with_retry(payload: dict, headers: dict) -> dict:
    """POST with exponential backoff on 429 / 5xx / transport errors."""
    backoff = _INITIAL_BACKOFF
    last_error: Exception | None = None

    for attempt in range(_MAX_RETRIES):
        try:
            response = httpx.post(GITHUB_MODELS_URL, json=payload, headers=headers, timeout=60.0)
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code == 429 or exc.response.status_code >= 500:
                wait = _get_retry_delay(exc.response, backoff)
                logger.warning(
                    "Embedding API returned %d (attempt %d/%d), retrying in %.1fs",
                    exc.response.status_code,
                    attempt + 1,
                    _MAX_RETRIES,
                    wait,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, _MAX_BACKOFF)
                continue
            raise
        except httpx.TransportError as exc:
            last_error = exc
            logger.warning(
                "Embedding API transport error: %s (attempt %d/%d), retrying in %.1fs",
                exc,
                attempt + 1,
                _MAX_RETRIES,
                backoff,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)

    if last_error is not None:
        raise last_error
    raise RuntimeError("No retries attempted")


def _get_retry_delay(response: httpx.Response, default_backoff: float) -> float:
    """Extract delay from Retry-After header, falling back to exponential backoff."""
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return min(float(retry_after), _MAX_BACKOFF)
        except ValueError:
            pass
    return default_backoff


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
