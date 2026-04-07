"""Copilot API client — uses gh CLI OAuth token for authentication."""

import subprocess

import httpx

API_URL = "https://api.githubcopilot.com/chat/completions"
HEADERS = {
    "Content-Type": "application/json",
    "Copilot-Integration-Id": "vscode-chat",
    "Editor-Version": "vscode/1.100.0",
}


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
) -> tuple[str, str]:
    """Send a chat completion request to the Copilot API."""
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

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            API_URL,
            headers={**HEADERS, "Authorization": f"Bearer {token}"},
            json=payload,
        )
        resp.raise_for_status()

    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    model_used = data.get("model", model)
    return content, model_used
