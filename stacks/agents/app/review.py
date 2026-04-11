"""PR review orchestrator — ties together git, copilot, and github modules."""

import logging
import re
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from copilot import TaskError, run_copilot
from git import cleanup_worktree, create_worktree
from github import (
    bot_login,
    comment_on_issue,
    dismiss_stale_reviews,
    get_issue,
    get_pr,
    get_unresolved_threads,
    post_review,
)

logger = logging.getLogger(__name__)


# ── Review output schema (single source of truth) ──────────────────────


class ReviewComment(BaseModel):
    """A single inline review comment attached to a file and line."""

    path: str
    line: int
    start_line: int | None = None
    body: str

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "path": "compose.yaml",
                    "line": 4,
                    "body": (
                        "🚫 **Blocker** — Secret leakage via build context\n\n"
                        "**Problem**: Widening Docker build context to the repo root "
                        "sends the entire directory tree to the daemon.\n\n"
                        "**Impact**: Every file not excluded by .dockerignore is readable "
                        "in the build tarball.\n\n"
                        "**Fix**: Convert .dockerignore to a whitelist pattern."
                    ),
                }
            ]
        }
    }


class ReviewOutput(BaseModel):
    """Schema for the .copilot-review.json file produced by the CLI."""

    event: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]
    body: str = ""
    comments: list[ReviewComment] = Field(default_factory=list)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "event": "APPROVE",
                    "body": "✅ **Approved** — no issues found.\n\n---",
                    "comments": [],
                }
            ]
        }
    }


# ── Prompt templates ───────────────────────────────────────

# Matches "Fixes #123", "Closes #123", "Resolves #123" (case-insensitive)
_LINKED_ISSUE_RE = re.compile(r"(?:fix(?:es)?|close[sd]?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE)

REVIEW_PROMPT_TEMPLATE = """\
Review PR #{pr_number} in {repo}.

Use the bot-review skill for review guidelines and output format.

## PR Context

**{title}**
Base branch: `{base_branch}`

{description}
{linked_issues_section}\
{previous_review_section}\
"""

LINKED_ISSUES_SECTION = """
## Linked Issues

{issues}
"""

PREVIOUS_REVIEW_SECTION = """
## Unresolved Review Threads

Previous reviews raised the findings below. Check whether each is still present
in the current code. In your review summary, note which are fixed and which remain.
Only re-report issues that are still present as new inline comments.

{threads}
"""

REVIEW_OUTPUT_FILE = ".copilot-review.json"


# ── Helpers ────────────────────────────────────────────────


def _parse_linked_issues(text: str) -> list[int]:
    """Extract issue numbers from 'Fixes #N' / 'Closes #N' / 'Resolves #N' in text."""
    return sorted(set(int(m) for m in _LINKED_ISSUE_RE.findall(text)))


async def _fetch_linked_issues_section(repo: str, description: str) -> str:
    """Fetch linked issue bodies and format as a prompt section."""
    issue_numbers = _parse_linked_issues(description)
    if not issue_numbers:
        return ""

    parts = []
    for num in issue_numbers:
        try:
            issue = await get_issue(repo, num)
            title = issue.get("title", "")
            body = issue.get("body") or "_No body._"
            parts.append(f"### #{num}: {title}\n\n{body}")
        except Exception:
            logger.warning("Could not fetch linked issue #%d", num)
    if not parts:
        return ""
    return LINKED_ISSUES_SECTION.format(issues="\n\n".join(parts))


def _parse_review_file(review_file: Path) -> ReviewOutput:
    """Parse and validate the review JSON file written by the CLI.

    Returns a validated ReviewOutput model.
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
        return ReviewOutput.model_validate_json(raw)
    except Exception as exc:
        raise RuntimeError(
            f"Review file validation failed: {exc}\nContent (first 500 chars): {raw[:500]}"
        ) from exc


# ── Orchestrator ───────────────────────────────────────────


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

        pr_data = await get_pr(repo, pr_number)
        title = pr_data.get("title", "")
        description = pr_data.get("body") or "_No description provided._"
        base_branch = pr_data.get("base", {}).get("ref", "main")

        linked_issues_section = await _fetch_linked_issues_section(repo, description)
        threads = await get_unresolved_threads(repo, pr_number)
        previous_section = PREVIOUS_REVIEW_SECTION.format(threads=threads) if threads else ""
        prompt = REVIEW_PROMPT_TEMPLATE.format(
            pr_number=pr_number,
            repo=repo,
            title=title,
            description=description,
            base_branch=base_branch,
            linked_issues_section=linked_issues_section,
            previous_review_section=previous_section,
        )

        result = await run_copilot(
            worktree_path,
            prompt,
            model=model,
            effort=reasoning_effort,
        )

        # Everything after run_copilot can fail — wrap in a single handler
        # so premium request count is always preserved for metrics
        try:
            try:
                review_data = _parse_review_file(worktree_path / REVIEW_OUTPUT_FILE)
            except RuntimeError as exc:
                logger.error("Review output validation failed: %s", exc)
                await comment_on_issue(
                    repo,
                    pr_number,
                    f"⚠️ **Review failed** — CLI produced invalid output.\n\n```\n{exc}\n```",
                )
                raise

            body = review_data.body

            event = review_data.event
            downgraded = False

            # GitHub doesn't allow REQUEST_CHANGES or APPROVE on your own PR
            is_own_pr = pr_data.get("user", {}).get("login") == bot_login()
            if is_own_pr and event in ("REQUEST_CHANGES", "APPROVE"):
                logger.info("Using COMMENT instead of %s (bot's own PR)", event)
                event = "COMMENT"
                downgraded = True

            if result.stats_line:
                body += f"\n\n📊 {result.stats_line}"

            comments_dicts = [c.model_dump(exclude_none=True) for c in review_data.comments]
            await post_review(
                repo,
                pr_number,
                event=event,
                body=body,
                comments=comments_dicts or None,
            )

            # Best-effort cleanup — review is already posted, don't fail the task
            try:
                await dismiss_stale_reviews(repo, pr_number, keep_latest=not downgraded)
            except Exception:
                logger.warning("Failed to dismiss stale reviews on %s#%d", repo, pr_number)

        except TaskError:
            raise
        except Exception as exc:
            raise TaskError(str(exc), premium_requests=result.total_premium_requests) from exc

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
