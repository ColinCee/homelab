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
    app_slug = os.environ.get("GITHUB_APP_SLUG", "homelab-review-bot")
    return f"{app_slug}[bot]"


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
            logger.warning("GraphQL request failed: %d", resp.status_code)
            return ""

        data = resp.json()
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
            body = first.get("body", "").strip()
            thread_id = t["id"]
            lines.append(f"- **{path}:{line}** (thread {thread_id}) — {body}")

        return "\n".join(lines)


async def dismiss_stale_reviews(repo: str, pr_number: int) -> None:
    """Dismiss previous bot reviews, keeping the latest one (just posted)."""
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

    async with httpx.AsyncClient(timeout=30) as client:
        for review in bot_reviews[:-1]:
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


async def append_stats_to_review(repo: str, pr_number: int, stats_line: str) -> None:
    """Find the bot's latest review and append a stats footer."""
    if not stats_line:
        return

    token = await get_token()
    login = bot_login()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    reviews_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"

    reviews = await _fetch_all_reviews(repo, pr_number, token)
    bot_reviews = [r for r in reviews if r.get("user", {}).get("login") == login]
    if not bot_reviews:
        logger.warning("No reviews from %s found to append stats to", login)
        return

    latest = bot_reviews[-1]
    review_id = latest["id"]
    current_body = latest.get("body", "")
    updated_body = f"{current_body}\n{stats_line}"

    update_url = f"{reviews_url}/{review_id}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(update_url, headers=headers, json={"body": updated_body})
        if resp.status_code == 200:
            logger.info("Appended stats to review %d", review_id)
        else:
            logger.warning("Failed to update review with stats: %d", resp.status_code)


async def count_bot_reviews(repo: str, pr_number: int) -> int:
    """Count active (non-dismissed) bot reviews on a PR."""
    token = await get_token()
    login = bot_login()
    reviews = await _fetch_all_reviews(repo, pr_number, token)
    return sum(
        1
        for r in reviews
        if r.get("user", {}).get("login") == login
        and r.get("state") in ("CHANGES_REQUESTED", "APPROVED", "COMMENTED")
    )
