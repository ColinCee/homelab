"""PR review orchestrator — sets up worktree, runs CLI with GitHub access."""

import logging
import re
import time

from models import TaskResult
from services.copilot import run_copilot
from services.git import cleanup_worktree, create_worktree
from services.github import (
    get_issue,
    get_pr,
    get_token,
)
from trust import is_trusted_content_author

logger = logging.getLogger(__name__)


# ── Prompt ─────────────────────────────────────────────────

_LINKED_ISSUE_RE = re.compile(
    r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s+#(\d+)",
    re.IGNORECASE,
)

REVIEW_PROMPT_TEMPLATE = """\
Review PR #{pr_number} in {repo}.

## {title}

{description}

Base branch: `{base_branch}`
{linked_issues_section}
Use the bot-review skill for guidelines on how to review and post your findings.
"""

LINKED_ISSUES_SECTION = """
## Linked Issues

{issues}
"""


# ── Helpers ────────────────────────────────────────────────


def _parse_linked_issues(text: str) -> list[int]:
    """Extract issue numbers from 'Fixes #N' / 'Closes #N' / 'Resolves #N' in text."""
    return sorted(set(int(m) for m in _LINKED_ISSUE_RE.findall(text)))


async def _fetch_linked_issues_section(repo: str, description: str) -> str:
    """Fetch linked issue bodies and format as a prompt section.

    Only includes issues authored by trusted roles to prevent prompt
    injection via attacker-controlled issue bodies.
    """
    issue_numbers = _parse_linked_issues(description)
    if not issue_numbers:
        return ""

    parts = []
    for num in issue_numbers:
        try:
            issue = await get_issue(repo, num)
            if not is_trusted_content_author(issue):
                logger.warning(
                    "Skipping linked issue #%d — author not trusted for prompt injection",
                    num,
                )
                continue
            title = issue.title
            body = issue.body or "_No body._"
            parts.append(f"### #{num}: {title}\n\n{body}")
        except Exception:
            logger.warning("Could not fetch linked issue #%d", num)
    if not parts:
        return ""
    return LINKED_ISSUES_SECTION.format(issues="\n\n".join(parts))


# ── Orchestrator ───────────────────────────────────────────


async def review_pr(
    *,
    repo: str,
    pr_number: int,
    model: str = "gpt-5.4",
    reasoning_effort: str = "high",
    session_id: str | None = None,
) -> TaskResult:
    """Review pipeline: worktree → Copilot CLI (posts review directly) → stats.

    The CLI has full GitHub access and posts the review itself via `gh`.
    The orchestrator only sets up the environment and collects stats.
    """
    logger.info("Starting review for %s#%d (model=%s)", repo, pr_number, model)
    start = time.monotonic()
    repo_url = f"https://github.com/{repo}.git"
    token = await get_token()

    try:
        pr_data = await get_pr(repo, pr_number)
        title = pr_data.title
        description = pr_data.body or "_No description provided._"
        base_branch = pr_data.base.ref if pr_data.base and pr_data.base.ref else "main"
        head_ref = pr_data.head.ref if pr_data.head else None
        head_repo = pr_data.head.repo.full_name if pr_data.head and pr_data.head.repo else None
        if head_repo != repo:
            raise ValueError(
                f"PR #{pr_number} is from fork '{head_repo}' — refusing to review with GH_TOKEN"
            )

        worktree_path = await create_worktree(pr_number, repo_url, head_ref=head_ref)

        linked_issues_section = await _fetch_linked_issues_section(repo, description)
        prompt = REVIEW_PROMPT_TEMPLATE.format(
            pr_number=pr_number,
            repo=repo,
            title=title,
            description=description,
            base_branch=base_branch,
            linked_issues_section=linked_issues_section,
        )

        result = await run_copilot(
            worktree_path,
            prompt,
            stage="review",
            model=model,
            effort=reasoning_effort,
            session_id=session_id,
            github_token=token,
        )

        elapsed = time.monotonic() - start
        logger.info("Review complete for %s#%d in %.1fs", repo, pr_number, elapsed)

        return TaskResult(
            status="complete",
            repo=repo,
            model=model,
            elapsed_seconds=elapsed,
            api_time_seconds=result.api_time_seconds,
            reasoning_effort=reasoning_effort,
            premium_requests=result.total_premium_requests,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cached_tokens=result.cached_tokens,
            reasoning_tokens=result.reasoning_tokens,
            cli_calls=1,
            models=result.models,
            tokens_line=result.tokens_line,
            session_id=result.session_id,
        )

    finally:
        await cleanup_worktree(pr_number)
