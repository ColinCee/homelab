"""Copilot API client — uses gh CLI OAuth token for authentication."""

import logging
import subprocess
from dataclasses import dataclass

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

API_URL = "https://api.githubcopilot.com/chat/completions"
HEADERS = {
    "Content-Type": "application/json",
    "Copilot-Integration-Id": "vscode-chat",
    "Editor-Version": "vscode/1.100.0",
}


@dataclass
class ChatResult:
    """Structured result from a Copilot API chat completion."""

    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    reasoning_tokens: int
    cached_tokens: int


def get_token() -> str:
    """Get OAuth token from gh CLI (auto-refreshes)."""
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
    return result.stdout.strip()


async def chat(
    *,
    system: str,
    user: str,
    model: str = "gpt-5.4",
    reasoning_effort: str = "high",
    response_schema: type[BaseModel] | None = None,
) -> ChatResult:
    """Send a chat completion request to the Copilot API.

    If response_schema is provided, uses strict structured output
    (json_schema response_format) to guarantee valid JSON matching
    the Pydantic model's schema.
    """
    token = get_token()
    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}

    if response_schema is not None:
        schema = _prepare_strict_schema(response_schema)
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": response_schema.__name__,
                "strict": True,
                "schema": schema,
            },
        }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            API_URL,
            headers={**HEADERS, "Authorization": f"Bearer {token}"},
            json=payload,
        )
        if resp.status_code >= 400:
            logger.error("Copilot API error %d: %s", resp.status_code, resp.text)
        resp.raise_for_status()
        logger.debug("Copilot API response: %s", resp.text)

    data = resp.json()
    usage = data.get("usage", {})
    prompt_details = usage.get("prompt_tokens_details", {})

    return ChatResult(
        content=data["choices"][0]["message"]["content"],
        model=data.get("model", model),
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
        reasoning_tokens=usage.get("reasoning_tokens", 0),
        cached_tokens=prompt_details.get("cached_tokens", 0),
    )


def _prepare_strict_schema(model: type[BaseModel]) -> dict:
    """Convert a Pydantic model to a strict-mode JSON schema.

    Strict mode requires:
    - additionalProperties: false on every object
    - All properties listed in required (use anyOf with null for optionals)
    - No default, title, or description fields
    """
    schema = model.model_json_schema()
    _strip_for_strict(schema)
    # Clean top-level metadata
    for key in ("title", "description"):
        schema.pop(key, None)
    return schema


def _strip_for_strict(schema: dict) -> None:
    """Recursively transform a schema for strict mode compliance."""
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
        # All properties must be required
        schema["required"] = list(schema.get("properties", {}).keys())
        for prop in schema.get("properties", {}).values():
            prop.pop("default", None)
            prop.pop("title", None)
            prop.pop("description", None)
            _strip_for_strict(prop)
    if "items" in schema:
        _strip_for_strict(schema["items"])
    for variant in schema.get("anyOf", []):
        _strip_for_strict(variant)
    # Pydantic v2 puts nested models in $defs
    for defn in schema.get("$defs", {}).values():
        defn.pop("title", None)
        defn.pop("description", None)
        _strip_for_strict(defn)
