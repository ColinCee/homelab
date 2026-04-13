"""GitHub API — token management and REST helpers."""

import logging

import httpx

logger = logging.getLogger(__name__)

# Token provided per-request by the workflow (via actions/create-github-app-token).
# Global is fine — runs are serialized per task type in a single-user system.
_active_token: str | None = None


def set_token(token: str) -> None:
    """Store the GitHub token provided by the calling workflow."""
    global _active_token
    _active_token = token


async def get_token() -> str:
    """Return the active GitHub token, set by the workflow via set_token().

    Raises RuntimeError if no token has been provided.
    """
    if not _active_token:
        raise RuntimeError("No GitHub token available — workflow must pass github_token in request")
    return _active_token


def reset_token_cache() -> None:
    """Clear the active token (for testing)."""
    global _active_token
    _active_token = None


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
