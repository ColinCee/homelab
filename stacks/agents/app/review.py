"""PR review logic — orchestrates worktree, Copilot CLI, and cleanup."""

import logging
import time

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

**Network note:** You are behind an egress proxy. Only github.com, \
api.githubcopilot.com, githubusercontent.com, and common documentation sites \
are reachable.
"""


async def review_pr(
    *,
    repo: str,
    pr_number: int,
    model: str = "gpt-5.4",
    reasoning_effort: str = "high",
) -> dict:
    """Full review pipeline: worktree → Copilot CLI (reviews + posts) → cleanup."""
    logger.info("Starting review for %s#%d (model=%s)", repo, pr_number, model)
    start = time.monotonic()
    repo_url = f"https://github.com/{repo}.git"

    token = await get_installation_token()
    worktree_path = await create_worktree(pr_number, repo_url)

    try:
        prompt = REVIEW_PROMPT_TEMPLATE.format(pr_number=pr_number, repo=repo)

        await run_copilot(
            worktree_path,
            prompt,
            model=model,
            effort=reasoning_effort,
            gh_token=token,
        )

        elapsed = time.monotonic() - start
        logger.info("Review complete for %s#%d in %.1fs", repo, pr_number, elapsed)

        return {
            "model": model,
            "elapsed_seconds": elapsed,
            "reasoning_effort": reasoning_effort,
        }

    finally:
        await cleanup_worktree(pr_number)
