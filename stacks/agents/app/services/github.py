"""GitHub API — App auth and REST helpers."""

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


async def get_token() -> str:
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


_APP_SLUG = "colins-homelab-bot"
_BOT_USER_ID = "274352150"  # stable across app renames


def bot_login() -> str:
    """The bot's GitHub login (e.g. 'colins-homelab-bot[bot]')."""
    return f"{_APP_SLUG}[bot]"


def bot_email() -> str:
    """GitHub noreply email for the bot, used as git commit author."""
    return f"{_BOT_USER_ID}+{bot_login()}@users.noreply.github.com"


# ── Issue / PR read ────────────────────────────────────────


async def get_issue(repo: str, issue_number: int) -> dict:
    """Fetch issue details (title, body, labels)."""
    token = await get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"https://api.github.com/repos/{repo}/issues/{issue_number}",
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()


async def close_issue(repo: str, issue_number: int) -> None:
    """Close an issue as completed."""
    token = await get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(
            f"https://api.github.com/repos/{repo}/issues/{issue_number}",
            headers=headers,
            json={"state": "closed", "state_reason": "completed"},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to close issue #{issue_number} on {repo}: HTTP {resp.status_code}"
            )


async def get_pr(repo: str, pr_number: int) -> dict:
    """Fetch PR details (head branch, base, state)."""
    token = await get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()


async def find_pr_by_branch(repo: str, branch: str) -> dict | None:
    """Find the most recently updated PR for a branch (any state)."""
    token = await get_token()
    owner = repo.split("/")[0]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"https://api.github.com/repos/{repo}/pulls",
            headers=headers,
            params={
                "head": f"{owner}:{branch}",
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "per_page": 1,
            },
        )
        resp.raise_for_status()
        pulls = resp.json()
        return pulls[0] if pulls else None


# ── Comments ───────────────────────────────────────────────


async def comment_on_issue(repo: str, issue_number: int, body: str) -> int:
    """Post a comment on an issue or PR and return the comment ID."""
    token = await get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments",
            headers=headers,
            json={"body": body},
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Failed to comment on {repo}#{issue_number}: HTTP {resp.status_code}"
            )
        data = resp.json()
        comment_id = data.get("id")
        if not isinstance(comment_id, int):
            raise RuntimeError(f"Failed to comment on {repo}#{issue_number}: missing comment ID")
        return comment_id


async def update_comment(repo: str, comment_id: int, body: str) -> None:
    """Edit an existing issue or PR comment."""
    token = await get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(
            f"https://api.github.com/repos/{repo}/issues/comments/{comment_id}",
            headers=headers,
            json={"body": body},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to update comment {comment_id} on {repo}: HTTP {resp.status_code}"
            )


async def find_issue_comment_by_body_prefix(
    repo: str, issue_number: int, body_prefix: str
) -> int | None:
    """Find the latest bot-authored issue/PR comment whose body starts with a prefix."""
    token = await get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    login = bot_login()
    comments: list[dict] = []
    page = 1

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            resp = await client.get(
                f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments",
                headers=headers,
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            comments.extend(batch)
            if len(batch) < 100:
                break
            page += 1

    for comment in reversed(comments):
        comment_id = comment.get("id")
        body = comment.get("body")
        author = comment.get("user", {}).get("login")
        if author != login or not isinstance(body, str) or not isinstance(comment_id, int):
            continue
        if body.startswith(body_prefix):
            return comment_id

    return None
