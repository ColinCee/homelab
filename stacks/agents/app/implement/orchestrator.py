"""Issue implementation orchestrator — dispatches to Copilot CLI with full repo access."""

import contextlib
import logging
import time

from services.copilot import TaskError, run_copilot
from services.git import cleanup_branch_worktree, create_branch_worktree
from services.github import (
    TRUSTED_ROLES,
    close_issue,
    comment_on_issue,
    find_pr_by_branch,
    get_issue,
    get_token,
)
from stats import STATUS_EMOJI, format_stage_stats

logger = logging.getLogger(__name__)


def _monotonic() -> float:
    return time.monotonic()


IMPLEMENT_PROMPT_TEMPLATE = """\
Implement the following GitHub issue in {repo}.

## Issue #{issue_number}: {title}

{body}

Use the bot-implement skill for guidelines on how to make changes.
"""


async def implement_issue(
    *,
    repo: str,
    issue_number: int,
    model: str = "gpt-5.4",
    reasoning_effort: str = "high",
    issue: dict | None = None,
) -> dict:
    """Implement a GitHub issue: set up worktree → CLI handles everything → check result.

    The CLI owns the full lifecycle: commit, push, create PR, self-review, fix,
    mark ready, and merge. The orchestrator only validates trust, sets up the
    environment, and collects stats.
    """
    logger.info("Implementing issue %s#%d", repo, issue_number)
    start = _monotonic()
    repo_url = f"https://github.com/{repo}.git"
    branch_name = f"agent/issue-{issue_number}"

    token = await get_token()
    issue_data = issue
    if issue_data is None:
        issue_data = await get_issue(repo, issue_number)

    author_role = issue_data.get("author_association", "NONE")
    if author_role not in TRUSTED_ROLES:
        raise ValueError(
            f"Issue #{issue_number} author has role '{author_role}' — "
            f"only {TRUSTED_ROLES} are trusted for autonomous implementation"
        )

    total_premium_requests = 0
    result_dict: dict | None = None

    try:
        worktree_path = await create_branch_worktree(branch_name, repo_url)

        prompt = IMPLEMENT_PROMPT_TEMPLATE.format(
            repo=repo,
            issue_number=issue_number,
            title=issue_data["title"],
            body=issue_data.get("body") or "(no description)",
        )

        result = await run_copilot(
            worktree_path,
            prompt,
            model=model,
            effort=reasoning_effort,
            github_token=token,
        )
        total_premium_requests += result.total_premium_requests

        # Check what the CLI accomplished
        owner = repo.split("/")[0]
        pr_data = await find_pr_by_branch(repo, f"{owner}:{branch_name}")

        elapsed = _monotonic() - start

        if pr_data:
            pr_number = pr_data["number"]
            pr_url = pr_data["html_url"]
            merged = pr_data.get("merged_at") is not None or pr_data.get("merged", False)

            if merged:
                with contextlib.suppress(Exception):
                    await close_issue(repo, issue_number)

                result_dict = {
                    "status": "complete",
                    "pr_number": pr_number,
                    "pr_url": pr_url,
                    "merged": True,
                    "elapsed_seconds": elapsed,
                    "premium_requests": total_premium_requests,
                    "session_id": result.session_id,
                }
            else:
                result_dict = {
                    "status": "partial",
                    "pr_number": pr_number,
                    "pr_url": pr_url,
                    "merged": False,
                    "elapsed_seconds": elapsed,
                    "premium_requests": total_premium_requests,
                    "session_id": result.session_id,
                    "error": "CLI created PR but did not merge — needs manual attention",
                }
        else:
            result_dict = {
                "status": "failed",
                "pr_number": None,
                "pr_url": None,
                "merged": False,
                "elapsed_seconds": elapsed,
                "premium_requests": total_premium_requests,
                "session_id": result.session_id,
                "error": "CLI did not create a PR",
            }

        return result_dict

    except TaskError:
        raise
    except Exception as exc:
        raise TaskError(str(exc), premium_requests=total_premium_requests) from exc

    finally:
        if result_dict and result_dict.get("pr_number"):
            with contextlib.suppress(Exception):
                stats = format_stage_stats(
                    premium_requests=total_premium_requests,
                    elapsed_seconds=_monotonic() - start,
                    effort=reasoning_effort,
                )
                status = result_dict.get("status", "unknown")
                emoji = STATUS_EMOJI.get(status, "❓")
                await comment_on_issue(
                    repo,
                    result_dict["pr_number"],
                    f"{emoji} **Implementation {status}**\n{stats}",
                )
        await cleanup_branch_worktree(branch_name)
