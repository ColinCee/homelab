"""Issue implementation orchestrator — dispatches to Copilot CLI with full repo access."""

import logging
import time
from datetime import UTC, datetime

from models import GitHubIssue, GitHubPullRequest, TaskResult
from services.copilot import CLIResult, TaskError, run_copilot
from services.git import cleanup_branch_worktree, create_branch_worktree
from services.github import (
    close_issue,
    find_pr_by_branch,
    get_issue,
    get_token,
    safe_comment,
)
from stats import STATUS_EMOJI, cli_stage_stats
from trust import is_trusted_content_author

logger = logging.getLogger(__name__)


def _monotonic() -> float:
    return time.monotonic()


def _utcnow() -> datetime:
    return datetime.now(UTC)


IMPLEMENT_PROMPT_TEMPLATE = """\
Implement the following GitHub issue in {repo}.

## Issue #{issue_number}: {title}

{body}

Use the bot-implement skill for guidelines on how to make changes.
"""


def _is_stale_pr(pr_data: GitHubPullRequest, run_start: datetime) -> bool:
    """Check if a PR predates the current run (reused branch name).

    A PR is stale if it was closed without merging, or merged before
    this run started (leftover from a previous implementation attempt).
    """
    state = pr_data.state
    merged_at_str = pr_data.merged_at

    # Closed without merging — definitely stale
    if state == "closed" and not merged_at_str:
        return True

    # Merged before this run started — leftover from a previous run
    if merged_at_str:
        merged_at = datetime.fromisoformat(merged_at_str.replace("Z", "+00:00"))
        if merged_at < run_start:
            return True

    return False


def _build_result(
    pr_data: GitHubPullRequest | None,
    elapsed: float,
    premium_requests: int,
    cli_result: CLIResult,
    repo: str,
) -> TaskResult:
    """Build a TaskResult from PR state after CLI completes."""
    if not pr_data:
        return TaskResult(
            status="failed",
            error="CLI did not create a PR",
            repo=repo,
            elapsed_seconds=elapsed,
            premium_requests=premium_requests,
            api_time_seconds=cli_result.api_time_seconds,
            models=cli_result.models,
            tokens_line=cli_result.tokens_line,
            session_id=cli_result.session_id,
        )

    merged = pr_data.merged_at is not None or pr_data.merged

    if merged:
        return TaskResult(
            status="complete",
            merged=True,
            pr_number=pr_data.number,
            pr_url=pr_data.html_url,
            repo=repo,
            elapsed_seconds=elapsed,
            premium_requests=premium_requests,
            api_time_seconds=cli_result.api_time_seconds,
            models=cli_result.models,
            tokens_line=cli_result.tokens_line,
            session_id=cli_result.session_id,
        )

    if pr_data.auto_merge is not None:
        return TaskResult(
            status="complete",
            merged=False,
            auto_merge=True,
            pr_number=pr_data.number,
            pr_url=pr_data.html_url,
            repo=repo,
            elapsed_seconds=elapsed,
            premium_requests=premium_requests,
            api_time_seconds=cli_result.api_time_seconds,
            models=cli_result.models,
            tokens_line=cli_result.tokens_line,
            session_id=cli_result.session_id,
        )

    return TaskResult(
        status="partial",
        merged=False,
        error="CLI created PR but did not merge — needs manual attention",
        pr_number=pr_data.number,
        pr_url=pr_data.html_url,
        repo=repo,
        elapsed_seconds=elapsed,
        premium_requests=premium_requests,
        api_time_seconds=cli_result.api_time_seconds,
        models=cli_result.models,
        tokens_line=cli_result.tokens_line,
        session_id=cli_result.session_id,
    )


async def implement_issue(
    *,
    repo: str,
    issue_number: int,
    model: str = "gpt-5.4",
    reasoning_effort: str = "high",
    issue: GitHubIssue | None = None,
) -> TaskResult:
    """Implement a GitHub issue: set up worktree → CLI handles everything → check result.

    The CLI owns the full lifecycle: commit, push, create PR, wait for CI,
    mark ready, and merge. The orchestrator only validates trust, sets up the
    environment, and collects stats.
    """
    logger.info("Implementing issue %s#%d", repo, issue_number)
    start = _monotonic()
    start_wall = _utcnow()
    repo_url = f"https://github.com/{repo}.git"
    branch_name = f"agent/issue-{issue_number}"

    token = await get_token()
    issue_data = issue
    if issue_data is None:
        issue_data = await get_issue(repo, issue_number)

    if not is_trusted_content_author(issue_data):
        raise ValueError(
            f"Issue #{issue_number} author is not trusted — "
            "refusing to inject untrusted content into CLI prompt"
        )

    total_premium_requests = 0
    cli_result: CLIResult | None = None
    result: TaskResult | None = None

    try:
        worktree_path = await create_branch_worktree(branch_name, repo_url)

        prompt = IMPLEMENT_PROMPT_TEMPLATE.format(
            repo=repo,
            issue_number=issue_number,
            title=issue_data.title,
            body=issue_data.body or "(no description)",
        )

        cli_result = await run_copilot(
            worktree_path,
            prompt,
            model=model,
            effort=reasoning_effort,
            github_token=token,
        )
        total_premium_requests += cli_result.total_premium_requests

        pr_data = await find_pr_by_branch(repo, branch_name)

        if pr_data and _is_stale_pr(pr_data, start_wall):
            logger.warning(
                "Ignoring stale PR #%d for %s (state=%s, merged_at=%s)",
                pr_data.number,
                branch_name,
                pr_data.state,
                pr_data.merged_at,
            )
            pr_data = None

        elapsed = _monotonic() - start
        result = _build_result(pr_data, elapsed, total_premium_requests, cli_result, repo)

        if result.merged:
            try:
                await close_issue(repo, issue_number)
            except Exception:
                logger.warning(
                    "Failed to close issue %s#%d after merged PR",
                    repo,
                    issue_number,
                    exc_info=True,
                )

        return result

    except TaskError:
        raise
    except Exception as exc:
        raise TaskError(str(exc), premium_requests=total_premium_requests) from exc

    finally:
        if result and result.pr_number is not None:
            stats = cli_stage_stats(cli_result, effort=reasoning_effort) if cli_result else ""
            emoji = STATUS_EMOJI.get(result.status, "❓")
            await safe_comment(
                repo,
                result.pr_number,
                f"{emoji} **Implementation {result.status}**\n{stats}",
            )
        await cleanup_branch_worktree(branch_name)
