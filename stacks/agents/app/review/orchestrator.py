"""PR review orchestrator — sets up worktree, runs CLI with GitHub access."""

import logging
import re
import time

from services.copilot import run_copilot
from services.git import cleanup_worktree, create_worktree
from services.github import (
    TRUSTED_ROLES,
    get_issue,
    get_pr,
    get_token,
)

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


# ── Orchestrator ───────────────────────────────────────────


async def review_pr(
    *,
    repo: str,
    pr_number: int,
    model: str = "gpt-5.4",
    reasoning_effort: str = "high",
    session_id: str | None = None,
) -> dict:
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
        title = pr_data.get("title", "")
        description = pr_data.get("body") or "_No description provided._"
        base_branch = pr_data.get("base", {}).get("ref", "main")
        head_ref = pr_data.get("head", {}).get("ref")
        head_repo = pr_data.get("head", {}).get("repo", {}).get("full_name")
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
            model=model,
            effort=reasoning_effort,
            session_id=session_id,
            github_token=token,
        )

        elapsed = time.monotonic() - start
        logger.info("Review complete for %s#%d in %.1fs", repo, pr_number, elapsed)

        return {
            "status": "complete",
            "model": model,
            "elapsed_seconds": elapsed,
            "api_time_seconds": result.api_time_seconds,
            "reasoning_effort": reasoning_effort,
            "premium_requests": result.total_premium_requests,
            "models": result.models,
            "tokens_line": result.tokens_line,
            "session_id": result.session_id,
        }

    finally:
        await cleanup_worktree(pr_number)
