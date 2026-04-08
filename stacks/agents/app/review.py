"""PR review orchestrator — ties together git, copilot, and github modules."""

import logging
import time

from copilot import run_copilot
from git import cleanup_worktree, create_worktree
from github import (
    append_stats_to_review,
    count_bot_reviews,
    dismiss_stale_reviews,
    get_token,
    get_unresolved_threads,
)

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


async def review_pr(
    *,
    repo: str,
    pr_number: int,
    model: str = "gpt-5.4",
    reasoning_effort: str = "high",
) -> dict:
    """Full review pipeline: worktree → Copilot CLI → post-review cleanup."""
    logger.info("Starting review for %s#%d (model=%s)", repo, pr_number, model)
    start = time.monotonic()
    repo_url = f"https://github.com/{repo}.git"

    token = await get_token()

    try:
        worktree_path = await create_worktree(pr_number, repo_url)

        reviews_before = await count_bot_reviews(repo, pr_number)

        threads = await get_unresolved_threads(repo, pr_number)
        previous_section = PREVIOUS_REVIEW_SECTION.format(threads=threads) if threads else ""
        prompt = REVIEW_PROMPT_TEMPLATE.format(
            pr_number=pr_number,
            repo=repo,
            previous_review_section=previous_section,
        )

        result = await run_copilot(
            worktree_path,
            prompt,
            model=model,
            effort=reasoning_effort,
            gh_token=token,
        )

        elapsed = time.monotonic() - start

        reviews_after = await count_bot_reviews(repo, pr_number)
        if reviews_after <= reviews_before:
            raise RuntimeError(
                f"Copilot CLI exited 0 but no new review was posted "
                f"(before={reviews_before}, after={reviews_after})"
            )

        await dismiss_stale_reviews(repo, pr_number)

        if result.stats_line:
            await append_stats_to_review(repo, pr_number, result.stats_line)

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
