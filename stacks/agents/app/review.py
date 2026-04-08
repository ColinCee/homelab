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

You have `gh` CLI available and authenticated. Use it to:
1. Read the PR details: `gh pr view {pr_number} --repo {repo} --json title,body,files`
2. Explore the codebase with grep and view to understand context
3. Post your review directly: `gh api repos/{repo}/pulls/{pr_number}/reviews --method POST ...`

Use the code-review skill for review guidelines and output format.

When posting the review via `gh api`, use this JSON structure:
- "event": "APPROVE" or "REQUEST_CHANGES"
- "body": your summary (end with a --- separator and bot attribution line)
- "comments": array of inline comments with "path", "line", and "body" fields

For inline comment bodies, prefix with severity emoji:
- 🚫 **Blocker** — for must-fix issues
- 💡 **Suggestion** — for non-blocking improvements
- ❓ **Question** — for clarification requests

Set event to REQUEST_CHANGES only if you have blocker comments.
"""


def _get_bot_login() -> str:
    """Derive the bot login from the GitHub App slug (set via env var)."""
    app_slug = os.environ.get("GITHUB_APP_SLUG", "homelab-review-bot")
    return f"{app_slug}[bot]"


async def _dismiss_stale_reviews(repo: str, pr_number: int, token: str) -> None:
    """Dismiss previous bot reviews, keeping the latest one (just posted)."""
    bot_login = _get_bot_login()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    reviews_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(reviews_url, headers=headers)
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
        resp = await client.get(reviews_url, headers=headers)
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

    worktree_path = await create_worktree(pr_number, repo_url)

    try:
        prompt = REVIEW_PROMPT_TEMPLATE.format(pr_number=pr_number, repo=repo)

        result = await run_copilot(
            worktree_path,
            prompt,
            model=model,
            effort=reasoning_effort,
            gh_token=token,
        )

        elapsed = time.monotonic() - start

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
