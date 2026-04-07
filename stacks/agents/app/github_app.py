"""GitHub App authentication — JWT generation and installation token exchange."""

import logging
import os
import time

import httpx
import jwt

logger = logging.getLogger(__name__)

_cached_token: str | None = None
_token_expires_at: float = 0


def _generate_jwt(app_id: str, private_key: str) -> str:
    """Generate a short-lived JWT for GitHub App authentication."""
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


async def get_installation_token() -> str:
    """Get a GitHub App installation access token, with caching.

    Reads config from environment:
      GITHUB_APP_ID — the App's numeric ID
      GITHUB_APP_INSTALLATION_ID — the installation's numeric ID
      GITHUB_APP_PRIVATE_KEY_PATH — path to the .pem file (default: /secrets/github-app.pem)
    """
    global _cached_token, _token_expires_at

    if _cached_token and time.time() < _token_expires_at - 300:
        return _cached_token

    app_id = os.environ["GITHUB_APP_ID"]
    installation_id = os.environ["GITHUB_APP_INSTALLATION_ID"]
    key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "/secrets/github-app.pem")

    with open(key_path) as f:
        private_key = f.read()

    jwt_token = _generate_jwt(app_id, private_key)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github+json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    _cached_token = data["token"]
    _token_expires_at = time.time() + 3600

    logger.info("Obtained new GitHub App installation token")
    return _cached_token


def reset_token_cache() -> None:
    """Clear the cached token (for testing)."""
    global _cached_token, _token_expires_at
    _cached_token = None
    _token_expires_at = 0
