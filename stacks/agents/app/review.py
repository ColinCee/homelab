"""PR review logic — orchestrates worktree, Copilot CLI, and cleanup."""

import logging
import os
import time

import httpx

from copilot_cli import run_copilot
from github_app import get_installation_token
from worktree import cleanup_worktree, create_worktree

logger = logging.getLogger(__name__)

REVIEW_PROMPT_TEMPLATE = """\
Review PR #{pr_number} in {repo}.

Use the code-review skill for review guidelines and output format.
{previous_review_section}\
"""

PREVIOUS_REVIEW_SECTION = """
## Unresolved Review Threads

Previous reviews raised the findings below. Check whether each is still present
in the current code. In your review summary, note which are fixed and which remain.
Only re-report issues that are still present as new inline comments.

{threads}
"""


def _get_bot_login() -> str:
    """Derive the bot login from the GitHub App slug (set via env var)."""
    app_slug = os.environ.get("GITHUB_APP_SLUG", "homelab-review-bot")
    return f"{app_slug}[bot]"


async def _get_unresolved_threads(repo: str, pr_number: int, token: str) -> str:
    """Fetch all unresolved, non-outdated review threads via GraphQL."""
    owner, name = repo.split("/", 1)
    bot_login = _get_bot_login()

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

        # Only unresolved, non-outdated threads started by the bot
        lines = []
        for t in threads:
            if t["isResolved"] or t["isOutdated"]:
                continue
            comments = t.get("comments", {}).get("nodes", [])
            if not comments:
                continue
            first = comments[0]
            if first.get("author", {}).get("login") != bot_login:
                continue

            path = t.get("path", "?")
            line = t.get("line") or "?"
            body = first.get("body", "").strip()
            thread_id = t["id"]
            lines.append(f"- **{path}:{line}** (thread {thread_id}) — {body}")

        if not lines:
            return ""

        return PREVIOUS_REVIEW_SECTION.format(threads="\n".join(lines))


async def _dismiss_stale_reviews(repo: str, pr_number: int, token: str) -> None:
    """Dismiss previous bot reviews, keeping the latest one (just posted)."""
    bot_login = _get_bot_login()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    reviews_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(reviews_url, headers=headers, params={"per_page": 100})
        if resp.status_code != 200:
            logger.warning("Failed to fetch reviews for dismissal: %d", resp.status_code)
            return

        reviews = resp.json()
        bot_reviews = [
            r
            for r in reviews
            if r.get("user", {}).get("login") == bot_login
            and r.get("state") in ("CHANGES_REQUESTED", "APPROVED")
        ]

        # Keep the latest bot review (just posted), dismiss all older ones
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


async def _append_stats_to_review(repo: str, pr_number: int, stats_line: str, token: str) -> None:
    """Find the bot's latest review and append a stats footer."""
    if not stats_line:
        return

    bot_login = _get_bot_login()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    reviews_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(reviews_url, headers=headers, params={"per_page": 100})
        if resp.status_code != 200:
            logger.warning("Failed to fetch reviews for stats append: %d", resp.status_code)
            return

        reviews = resp.json()
        bot_reviews = [r for r in reviews if r.get("user", {}).get("login") == bot_login]
        if not bot_reviews:
            logger.warning("No reviews from %s found to append stats to", bot_login)
            return

        latest = bot_reviews[-1]
        review_id = latest["id"]
        current_body = latest.get("body", "")

        updated_body = f"{current_body}\n{stats_line}"

        update_url = f"{reviews_url}/{review_id}"
        resp = await client.put(update_url, headers=headers, json={"body": updated_body})
        if resp.status_code == 200:
            logger.info("Appended stats to review %d", review_id)
        else:
            logger.warning("Failed to update review with stats: %d", resp.status_code)


async def _count_bot_reviews(repo: str, pr_number: int, token: str) -> int:
    """Count the number of active (non-dismissed) bot reviews on a PR."""
    bot_login = _get_bot_login()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    reviews_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(reviews_url, headers=headers, params={"per_page": 100})
        if resp.status_code != 200:
            return 0
        return sum(
            1
            for r in resp.json()
            if r.get("user", {}).get("login") == bot_login
            and r.get("state") in ("CHANGES_REQUESTED", "APPROVED", "COMMENTED")
        )


async def review_pr(
    *,
    repo: str,
    pr_number: int,
    model: str = "gpt-5.4",
    reasoning_effort: str = "high",
) -> dict:
    """Full review pipeline: worktree → Copilot CLI (reviews + posts) → append stats → cleanup."""
    logger.info("Starting review for %s#%d (model=%s)", repo, pr_number, model)
    start = time.monotonic()
    repo_url = f"https://github.com/{repo}.git"

    token = await get_installation_token()

    try:
        worktree_path = await create_worktree(pr_number, repo_url)

        # Snapshot review count before running Copilot
        reviews_before = await _count_bot_reviews(repo, pr_number, token)

        previous_context = await _get_unresolved_threads(repo, pr_number, token)
        prompt = REVIEW_PROMPT_TEMPLATE.format(
            pr_number=pr_number,
            repo=repo,
            previous_review_section=previous_context,
        )

        result = await run_copilot(
            worktree_path,
            prompt,
            model=model,
            effort=reasoning_effort,
            gh_token=token,
        )

        elapsed = time.monotonic() - start

        # Verify the CLI actually posted a new review
        reviews_after = await _count_bot_reviews(repo, pr_number, token)
        if reviews_after <= reviews_before:
            raise RuntimeError(
                f"Copilot CLI exited 0 but no new review was posted "
                f"(before={reviews_before}, after={reviews_after})"
            )

        # Dismiss stale reviews only after a new one is successfully posted
        await _dismiss_stale_reviews(repo, pr_number, token)

        if result.stats_line:
            await _append_stats_to_review(repo, pr_number, result.stats_line, token)

        logger.info("Review complete for %s#%d in %.1fs", repo, pr_number, elapsed)

        return {
            "model": model,
            "elapsed_seconds": elapsed,
            "reasoning_effort": reasoning_effort,
            "premium_requests": result.total_premium_requests,
            "models": result.models,
        }

    finally:
        await cleanup_worktree(pr_number)
