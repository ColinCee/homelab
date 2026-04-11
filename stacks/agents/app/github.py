"""GitHub API — App auth, REST, and GraphQL helpers."""

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


def bot_login() -> str:
    """Derive the bot login from the GitHub App slug (set via env var)."""
    app_slug = os.environ.get("GITHUB_APP_SLUG", "colins-homelab-bot")
    return f"{app_slug}[bot]"


def bot_email() -> str:
    """GitHub noreply email for the bot, used as git commit author.

    Bot user ID (274352150) is stable across app renames.
    """
    bot_id = os.environ.get("GITHUB_APP_BOT_ID", "274352150")
    return f"{bot_id}+{bot_login()}@users.noreply.github.com"


async def _fetch_all_reviews(repo: str, pr_number: int, token: str) -> list[dict]:
    """Paginate through all reviews on a PR."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    reviews_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"
    all_reviews: list[dict] = []
    page = 1

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            resp = await client.get(
                reviews_url, headers=headers, params={"per_page": 100, "page": page}
            )
            if resp.status_code != 200:
                logger.warning("Failed to fetch reviews (page %d): %d", page, resp.status_code)
                break
            batch = resp.json()
            if not batch:
                break
            all_reviews.extend(batch)
            if len(batch) < 100:
                break
            page += 1

    return all_reviews


async def get_unresolved_threads(repo: str, pr_number: int) -> str:
    """Fetch unresolved, non-outdated bot review threads via GraphQL.

    Returns a formatted markdown string for inclusion in the review prompt,
    or empty string if no unresolved threads exist.
    """
    token = await get_token()
    owner, name = repo.split("/", 1)
    login = bot_login()

    query = """
    query($owner: String!, $name: String!, $pr: Int!) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $pr) {
          reviewThreads(first: 100) {
            nodes {
              id
              isResolved
              isOutdated
              path
              line
              comments(first: 20) {
                nodes {
                  author { login }
                  body
                }
              }
            }
          }
        }
      }
    }
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.github.com/graphql",
            headers=headers,
            json={
                "query": query,
                "variables": {"owner": owner, "name": name, "pr": pr_number},
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"GraphQL request failed: HTTP {resp.status_code}")

        data = resp.json()

        if "errors" in data:
            raise RuntimeError(f"GraphQL errors: {data['errors']}")

        threads = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )

        lines = []
        for t in threads:
            if t["isResolved"] or t["isOutdated"]:
                continue
            comments = t.get("comments", {}).get("nodes", [])
            if not comments:
                continue
            first = comments[0]
            if first.get("author", {}).get("login") != login:
                continue

            path = t.get("path", "?")
            line = t.get("line") or "?"
            thread_id = t["id"]
            # Only include bot-authored comments to prevent prompt injection
            # from untrusted PR participants replying in review threads
            bot_comments = [c for c in comments if c.get("author", {}).get("login") == login]
            thread_lines = [f"- **{path}:{line}** (thread {thread_id})"]
            for comment in bot_comments:
                body = comment.get("body", "").strip()
                thread_lines.append(f"  {body}")
            lines.append("\n".join(thread_lines))

        return "\n".join(lines)


# --- Pull Request Reviews ---
#
# GitHub has three distinct comment types on PRs:
#   1. PR comments — plain text on the timeline (Issues API), no merge impact
#   2. Review comments — inline code annotations, always part of a review
#   3. Reviews — submitted via the Reviews API with a verdict:
#      - APPROVED / CHANGES_REQUESTED — "stateful", affect branch protection
#      - COMMENT — informational only, no merge impact
#
# Only stateful reviews (APPROVED, CHANGES_REQUESTED) can be dismissed.
# Dismissal changes the state to DISMISSED, removing its merge-blocking effect.
# COMMENT reviews are permanent — they can't be dismissed or retracted.


async def dismiss_stale_reviews(repo: str, pr_number: int, *, keep_latest: bool = True) -> None:
    """Dismiss previous stateful bot reviews.

    Args:
        keep_latest: If True, keep the most recent stateful review (normal case).
            If False, dismiss ALL stateful reviews (used when the new review is a
            COMMENT that won't appear in the stateful list).
    """
    token = await get_token()
    login = bot_login()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    reviews_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"

    reviews = await _fetch_all_reviews(repo, pr_number, token)
    bot_reviews = [
        r
        for r in reviews
        if r.get("user", {}).get("login") == login
        and r.get("state") in ("CHANGES_REQUESTED", "APPROVED")
    ]

    to_dismiss = bot_reviews[:-1] if keep_latest else bot_reviews

    async with httpx.AsyncClient(timeout=30) as client:
        for review in to_dismiss:
            dismiss_url = f"{reviews_url}/{review['id']}/dismissals"
            resp = await client.put(
                dismiss_url,
                headers=headers,
                json={"message": "Superseded by new review."},
            )
            if resp.status_code == 200:
                logger.info("Dismissed stale review %d", review["id"])
            else:
                logger.warning("Failed to dismiss review %d: %d", review["id"], resp.status_code)


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


async def create_pull_request(
    repo: str, *, title: str, body: str, head: str, base: str = "main"
) -> dict:
    """Create a pull request. Returns PR data with number and html_url."""
    token = await get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://api.github.com/repos/{repo}/pulls",
            headers=headers,
            json={"title": title, "body": body, "head": head, "base": base},
        )
        resp.raise_for_status()
        return resp.json()


async def comment_on_issue(repo: str, issue_number: int, body: str) -> None:
    """Post a comment on an issue or PR."""
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


async def post_review(
    repo: str,
    pr_number: int,
    *,
    event: str,
    body: str,
    comments: list[dict] | None = None,
) -> dict:
    """Post a pull request review with optional inline comments.

    If GitHub rejects inline comments (e.g. invalid line numbers not in the
    diff), retries without them and appends comment text to the body instead.
    """
    token = await get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    payload: dict = {"event": event, "body": body}
    if comments:
        payload["comments"] = comments

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews",
            headers=headers,
            json=payload,
        )

        if resp.status_code == 422 and comments:
            logger.warning(
                "GitHub rejected inline comments (422), retrying without them: %s",
                resp.text[:500],
            )
            fallback_parts = [
                body,
                "",
                "---",
                "*Inline comments could not be posted (invalid line numbers). Included below:*",
                "",
            ]
            for c in comments:
                fallback_parts.append(
                    f"**{c.get('path', '?')}:{c.get('line', '?')}** — {c.get('body', '')}"
                )
                fallback_parts.append("")

            fallback_payload: dict = {"event": event, "body": "\n".join(fallback_parts)}
            resp = await client.post(
                f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews",
                headers=headers,
                json=fallback_payload,
            )

        resp.raise_for_status()
        return resp.json()
