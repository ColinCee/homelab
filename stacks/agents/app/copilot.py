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

    If response_schema is provided, uses structured output (response_format)
    to guarantee the response matches the Pydantic model's JSON schema.
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
        schema = response_schema.model_json_schema()
        # Strict mode requires additionalProperties: false at all levels
        _set_additional_properties_false(schema)
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


def _set_additional_properties_false(schema: dict) -> None:
    """Recursively set additionalProperties: false for strict mode."""
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
        for prop in schema.get("properties", {}).values():
            _set_additional_properties_false(prop)
    if "items" in schema:
        _set_additional_properties_false(schema["items"])
    for variant in schema.get("anyOf", []):
        _set_additional_properties_false(variant)
    # Handle $defs (Pydantic v2 puts nested models here)
    for defn in schema.get("$defs", {}).values():
        _set_additional_properties_false(defn)
