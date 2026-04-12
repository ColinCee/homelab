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
    TRUSTED_ROLES,
    bot_login,
    comment_on_issue,
    dismiss_stale_reviews,
    get_diff_valid_lines,
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
                        "in the build tarball — today that's .env, tomorrow anything "
                        "added to the repo root. Layer caching can persist these.\n\n"
                        "**Fix**: Convert .dockerignore to a whitelist pattern "
                        "(deny all, allow specific paths)."
                    ),
                },
                {
                    "path": "main.py",
                    "line": 25,
                    "body": (
                        "💡 **Suggestion** — Redundant API call\n\n"
                        "**Problem**: `get_pr()` is called twice — once for prompt "
                        "context, once for the bot-authored-PR check.\n\n"
                        "**Impact**: Adds ~200ms per review and creates a maintenance "
                        "trap — if the fetch logic changes, it needs updating in two "
                        "places.\n\n"
                        "**Fix**: Reuse the result from the first call."
                    ),
                },
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
                    "body": (
                        "✅ **Approved** — no issues found.\n\n"
                        "Solid approach — clean separation between orchestrator and CLI.\n\n---"
                    ),
                    "comments": [],
                },
                {
                    "event": "REQUEST_CHANGES",
                    "body": (
                        "🚫 **Changes requested** — see inline comments.\n\n"
                        "Widening the build context is the right call for mise access, "
                        "but it introduces a secret-leakage surface that needs a "
                        "whitelist-based .dockerignore.\n\n---"
                    ),
                    "comments": [
                        {
                            "path": "compose.yaml",
                            "line": 4,
                            "body": "🚫 **Blocker** — see ReviewComment examples",
                        }
                    ],
                },
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


def _format_review_threads(comments: list[ReviewComment]) -> str:
    """Format review comments as a thread summary for fix prompts and re-review context.

    Produces a format compatible with get_unresolved_threads() output so the fix
    prompt and PREVIOUS_REVIEW_SECTION work identically regardless of source.
    """
    if not comments:
        return ""
    return "\n".join(f"- **{c.path}:{c.line}**\n  {c.body}" for c in comments)


def _parse_linked_issues(text: str) -> list[int]:
    """Extract issue numbers from 'Fixes #N' / 'Closes #N' / 'Resolves #N' in text."""
    return sorted(set(int(m) for m in _LINKED_ISSUE_RE.findall(text)))


async def _fetch_linked_issues_section(repo: str, description: str) -> str:
    """Fetch linked issue bodies and format as a prompt section.

    Only includes issues authored by trusted roles (OWNER/MEMBER/COLLABORATOR)
    to prevent prompt injection via attacker-controlled issue bodies.
    """
    issue_numbers = _parse_linked_issues(description)
    if not issue_numbers:
        return ""

    parts = []
    for num in issue_numbers:
        try:
            issue = await get_issue(repo, num)
            author_role = issue.get("author_association", "NONE")
            if author_role not in TRUSTED_ROLES:
                logger.warning(
                    "Skipping linked issue #%d — author role '%s' not trusted",
                    num,
                    author_role,
                )
                continue
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


def _review_output_failure_comment(reason: str) -> str:
    return f"⚠️ **Review failed** — CLI produced invalid output.\n\n```\n{reason}\n```"


# ── Orchestrator ───────────────────────────────────────────


async def review_pr(
    *,
    repo: str,
    pr_number: int,
    model: str = "gpt-5.4",
    reasoning_effort: str = "high",
    previous_comments: str = "",
    session_id: str | None = None,
) -> dict:
    """Full review pipeline: worktree → Copilot CLI → read JSON → post review.

    Args:
        previous_comments: Thread summary from a prior review round. Used as
            fallback for PREVIOUS_REVIEW_SECTION when get_unresolved_threads()
            returns empty (e.g. self-PR where COMMENT reviews don't create
            resolvable threads).
        session_id: Session ID from a prior review. When provided, the CLI
            resumes the conversation so the reviewer has full memory of what
            it said in previous rounds.
    """
    logger.info("Starting review for %s#%d (model=%s)", repo, pr_number, model)
    start = time.monotonic()
    repo_url = f"https://github.com/{repo}.git"

    try:
        pr_data = await get_pr(repo, pr_number)
        title = pr_data.get("title", "")
        description = pr_data.get("body") or "_No description provided._"
        base_branch = pr_data.get("base", {}).get("ref", "main")
        head_ref = pr_data.get("head", {}).get("ref")
        # Only use the branch name directly for same-repo PRs. Fork PRs have
        # a branch name that exists in the fork, not in origin — fall back to
        # pull/N/head for those.
        head_repo = pr_data.get("head", {}).get("repo", {}).get("full_name")
        if head_repo != repo:
            head_ref = None

        worktree_path = await create_worktree(pr_number, repo_url, head_ref=head_ref)

        linked_issues_section = await _fetch_linked_issues_section(repo, description)
        threads = await get_unresolved_threads(repo, pr_number)
        if not threads and previous_comments:
            threads = previous_comments
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
            session_id=session_id,
        )

        # Everything after run_copilot can fail — wrap in a single handler
        # so premium request count is always preserved for metrics
        try:
            try:
                review_data = _parse_review_file(worktree_path / REVIEW_OUTPUT_FILE)
            except RuntimeError as exc:
                logger.warning("Review output validation failed, retrying once: %s", exc)
                retry_session_id = result.session_id or session_id
                review_file = worktree_path / REVIEW_OUTPUT_FILE
                review_file.unlink(missing_ok=True)

                retry_result = await run_copilot(
                    worktree_path,
                    prompt,
                    model=model,
                    effort=reasoning_effort,
                    session_id=retry_session_id,
                )
                retry_result.total_premium_requests += result.total_premium_requests
                result = retry_result

                try:
                    review_data = _parse_review_file(review_file)
                except RuntimeError as retry_exc:
                    logger.error("Review output validation failed after retry: %s", retry_exc)
                    await comment_on_issue(
                        repo,
                        pr_number,
                        _review_output_failure_comment(str(retry_exc)),
                    )
                    raise TaskError(
                        str(retry_exc),
                        premium_requests=result.total_premium_requests,
                        commented=True,
                    ) from retry_exc

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

            head_sha = pr_data.get("head", {}).get("sha")

            # Strip start_line — ranged comments need side/start_side which we
            # don't support yet. Single-line comments are reliable.
            comments_dicts = [
                {k: v for k, v in c.model_dump(exclude_none=True).items() if k != "start_line"}
                for c in review_data.comments
            ]

            # Filter comments to lines that actually appear in the diff —
            # GitHub returns 422 for lines outside diff hunks.
            if comments_dicts:
                valid_lines = await get_diff_valid_lines(repo, pr_number)
                valid_comments: list[dict] = []
                dropped_comments: list[dict] = []
                for c in comments_dicts:
                    if (c["path"], c["line"]) in valid_lines:
                        valid_comments.append(c)
                    else:
                        dropped_comments.append(c)
                        logger.warning(
                            "Comment on %s:%d not in diff — moving to body",
                            c["path"],
                            c["line"],
                        )

                if dropped_comments:
                    body += "\n\n---\n*Some comments reference lines outside the diff:*\n\n"
                    for c in dropped_comments:
                        body += f"**{c['path']}:{c['line']}** — {c['body']}\n\n"

                comments_dicts = valid_comments

            await post_review(
                repo,
                pr_number,
                event=event,
                body=body,
                comments=comments_dicts or None,
                commit_id=head_sha,
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
            "original_event": review_data.event,
            "review_threads": _format_review_threads(review_data.comments),
            "session_id": result.session_id,
        }

    finally:
        await cleanup_worktree(pr_number)
