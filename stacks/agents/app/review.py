"""PR review orchestrator — ties together git, copilot, and github modules."""

import json
import logging
import time
from pathlib import Path

from copilot import TaskError, run_copilot
from git import cleanup_worktree, create_worktree
from github import (
    bot_login,
    comment_on_issue,
    dismiss_stale_reviews,
    get_pr,
    get_unresolved_threads,
    post_review,
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

REVIEW_OUTPUT_FILE = ".copilot-review.json"
VALID_EVENTS = {"APPROVE", "REQUEST_CHANGES", "COMMENT"}


def _parse_review_file(review_file: Path) -> dict:
    """Parse and validate the review JSON file written by the CLI.

    Returns a validated dict with 'event', 'body', and 'comments' keys.
    Raises RuntimeError with a descriptive message on any validation failure.
    """
    if not review_file.exists():
        raise RuntimeError(
            f"Copilot CLI did not produce a review file ({REVIEW_OUTPUT_FILE} not found)"
        )

    raw = review_file.read_text().strip()

    # CLI occasionally wraps JSON in markdown code fences
    if raw.startswith("```"):
        lines = raw.splitlines()
        lines = [line for line in lines if not line.startswith("```")]
        raw = "\n".join(lines).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Review file is not valid JSON: {exc}\nContent (first 500 chars): {raw[:500]}"
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"Review file must be a JSON object, got {type(data).__name__}")

    event = data.get("event")
    if event not in VALID_EVENTS:
        raise RuntimeError(f"Invalid review event '{event}' — must be one of {VALID_EVENTS}")

    comments = data.get("comments", [])
    if not isinstance(comments, list):
        raise RuntimeError(f"'comments' must be a list, got {type(comments).__name__}")

    for i, c in enumerate(comments):
        if not isinstance(c, dict):
            raise RuntimeError(f"Comment {i} must be an object, got {type(c).__name__}")
        for required_key in ("path", "line", "body"):
            if required_key not in c:
                raise RuntimeError(f"Comment {i} missing required key '{required_key}'")

    return {"event": event, "body": data.get("body", ""), "comments": comments}


async def review_pr(
    *,
    repo: str,
    pr_number: int,
    model: str = "gpt-5.4",
    reasoning_effort: str = "high",
) -> dict:
    """Full review pipeline: worktree → Copilot CLI → read JSON → post review."""
    logger.info("Starting review for %s#%d (model=%s)", repo, pr_number, model)
    start = time.monotonic()
    repo_url = f"https://github.com/{repo}.git"

    try:
        worktree_path = await create_worktree(pr_number, repo_url)

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
        )

        try:
            review_data = _parse_review_file(worktree_path / REVIEW_OUTPUT_FILE)
        except RuntimeError as exc:
            logger.error("Review output validation failed: %s", exc)
            await comment_on_issue(
                repo,
                pr_number,
                f"⚠️ **Review failed** — CLI produced invalid output.\n\n```\n{exc}\n```",
            )
            raise TaskError(str(exc), premium_requests=result.total_premium_requests) from exc

        body = review_data["body"]

        event = review_data["event"]
        downgraded = False

        # GitHub doesn't allow REQUEST_CHANGES on your own PR — use COMMENT instead
        pr_data = await get_pr(repo, pr_number)
        if event == "REQUEST_CHANGES" and pr_data.get("user", {}).get("login") == bot_login():
            logger.info("Using COMMENT instead of REQUEST_CHANGES (bot's own PR)")
            event = "COMMENT"
            downgraded = True

        # Append stats as a collapsible footer — orchestrator-only metadata
        if result.stats_line:
            body += f"\n\n<details>\n<summary>📊 Stats</summary>\n\n{result.stats_line}\n</details>"

        try:
            await post_review(
                repo,
                pr_number,
                event=event,
                body=body,
                comments=review_data["comments"] or None,
            )
        except Exception as exc:
            raise TaskError(str(exc), premium_requests=result.total_premium_requests) from exc

        # When downgraded to COMMENT, dismiss ALL prior stateful reviews —
        # otherwise a stale APPROVE could linger since our COMMENT isn't stateful
        await dismiss_stale_reviews(repo, pr_number, keep_latest=not downgraded)

        elapsed = time.monotonic() - start
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
