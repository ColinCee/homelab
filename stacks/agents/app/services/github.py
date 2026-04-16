"""GitHub API — token management, REST helpers, and GraphQL queries."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

import httpx
from pydantic import TypeAdapter

from models import GitHubIssue, GitHubIssueComment, GitHubPullRequest, ReviewThread

logger = logging.getLogger(__name__)

# Token provided per-request by the workflow (via actions/create-github-app-token).
# ContextVar ensures each asyncio task has its own token copy, so concurrent
# review + implement runs can't overwrite each other's credentials.
_active_token: ContextVar[str | None] = ContextVar("github_token", default=None)


def set_token(token: str) -> None:
    """Store the GitHub token provided by the calling workflow."""
    _active_token.set(token)


async def get_token() -> str:
    """Return the active GitHub token, set by the workflow via set_token().

    Raises RuntimeError if no token has been provided.
    """
    token = _active_token.get()
    if not token:
        raise RuntimeError("No GitHub token available — workflow must pass github_token in request")
    return token


def reset_token_cache() -> None:
    """Clear the active token (for testing)."""
    _active_token.set(None)


_APP_SLUG = "colins-homelab-bot"
_BOT_USER_ID = "274352150"  # stable across app renames


def bot_login() -> str:
    """The bot's GitHub login (e.g. 'colins-homelab-bot[bot]')."""
    return f"{_APP_SLUG}[bot]"


def bot_email() -> str:
    """GitHub noreply email for the bot, used as git commit author."""
    return f"{_BOT_USER_ID}+{bot_login()}@users.noreply.github.com"


# ── Authenticated client ───────────────────────────────────


@asynccontextmanager
async def _client() -> AsyncIterator[httpx.AsyncClient]:
    """Yield an httpx client with GitHub auth headers."""
    token = await get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        yield client


_API = "https://api.github.com"


# ── Issue / PR read ────────────────────────────────────────


async def get_issue(repo: str, issue_number: int) -> GitHubIssue:
    """Fetch issue details (title, body, labels)."""
    async with _client() as client:
        resp = await client.get(f"{_API}/repos/{repo}/issues/{issue_number}")
        resp.raise_for_status()
        return GitHubIssue.model_validate(resp.json())


async def close_issue(repo: str, issue_number: int) -> None:
    """Close an issue as completed. Best-effort — logs on failure, never raises."""
    try:
        async with _client() as client:
            resp = await client.patch(
                f"{_API}/repos/{repo}/issues/{issue_number}",
                json={"state": "closed", "state_reason": "completed"},
            )
            if resp.status_code != 200:
                logger.warning(
                    "Failed to close issue #%d on %s: HTTP %d",
                    issue_number,
                    repo,
                    resp.status_code,
                )
    except Exception:
        logger.warning("Failed to close issue #%d on %s", issue_number, repo, exc_info=True)


async def get_pr(repo: str, pr_number: int) -> GitHubPullRequest:
    """Fetch PR details (head branch, base, state)."""
    async with _client() as client:
        resp = await client.get(f"{_API}/repos/{repo}/pulls/{pr_number}")
        resp.raise_for_status()
        return GitHubPullRequest.model_validate(resp.json())


async def find_pr_by_branch(repo: str, branch: str) -> GitHubPullRequest | None:
    """Find the most recently updated PR for a branch (any state)."""
    owner = repo.split("/")[0]
    async with _client() as client:
        resp = await client.get(
            f"{_API}/repos/{repo}/pulls",
            params={
                "head": f"{owner}:{branch}",
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "per_page": 1,
            },
        )
        resp.raise_for_status()
        pulls = TypeAdapter(list[GitHubPullRequest]).validate_python(resp.json())
        return pulls[0] if pulls else None


# ── Comments ───────────────────────────────────────────────


async def comment_on_issue(repo: str, issue_number: int, body: str) -> int:
    """Post a comment on an issue or PR and return the comment ID."""
    async with _client() as client:
        resp = await client.post(
            f"{_API}/repos/{repo}/issues/{issue_number}/comments",
            json={"body": body},
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Failed to comment on {repo}#{issue_number}: HTTP {resp.status_code}"
            )
        comment = GitHubIssueComment.model_validate(resp.json())
        return comment.id


async def safe_comment(repo: str, issue_number: int, body: str, **log_ctx: Any) -> None:
    """Post a comment, logging a warning on failure instead of raising."""
    try:
        await comment_on_issue(repo, issue_number, body)
    except Exception:
        logger.warning(
            "Failed to post comment on %s#%d",
            repo,
            issue_number,
            exc_info=True,
            extra=log_ctx,
        )


async def update_comment(repo: str, comment_id: int, body: str) -> None:
    """Edit an existing issue or PR comment."""
    async with _client() as client:
        resp = await client.patch(
            f"{_API}/repos/{repo}/issues/comments/{comment_id}",
            json={"body": body},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to update comment {comment_id} on {repo}: HTTP {resp.status_code}"
            )


async def find_issue_comment_by_body_prefix(
    repo: str, issue_number: int, body_prefix: str
) -> int | None:
    """Find the latest bot-authored issue/PR comment whose body starts with a prefix."""
    login = bot_login()
    comments: list[GitHubIssueComment] = []
    page = 1

    async with _client() as client:
        while True:
            resp = await client.get(
                f"{_API}/repos/{repo}/issues/{issue_number}/comments",
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            batch = TypeAdapter(list[GitHubIssueComment]).validate_python(resp.json())
            if not batch:
                break
            comments.extend(batch)
            if len(batch) < 100:
                break
            page += 1

    for comment in reversed(comments):
        body = comment.body
        author = comment.user.login if comment.user else ""
        if author != login or not isinstance(body, str):
            continue
        if body.startswith(body_prefix):
            return comment.id

    return None


# ── Review threads (GraphQL) ──────────────────────────────


_REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 1) {
            nodes { body }
          }
        }
      }
    }
  }
}
"""

_GRAPHQL = "https://api.github.com/graphql"


async def get_unresolved_review_threads(repo: str, pr_number: int) -> list[ReviewThread]:
    """Fetch unresolved, non-outdated review threads on a PR via GraphQL.

    Best-effort — returns an empty list on any failure so callers don't need try/except.
    """
    try:
        owner, name = repo.split("/", 1)
        async with _client() as client:
            resp = await client.post(
                _GRAPHQL,
                json={
                    "query": _REVIEW_THREADS_QUERY,
                    "variables": {"owner": owner, "repo": name, "pr": pr_number},
                },
            )
            resp.raise_for_status()
            data = resp.json()

        errors = data.get("errors")
        if errors:
            logger.warning(
                "GraphQL errors fetching review threads for %s#%d: %s",
                repo,
                pr_number,
                errors,
            )
            return []

        pr_data = data.get("data", {}).get("repository", {}).get("pullRequest")
        if not pr_data:
            return []

        threads: list[ReviewThread] = []
        for node in pr_data.get("reviewThreads", {}).get("nodes", []):
            if node.get("isResolved") or node.get("isOutdated"):
                continue
            first_comment = ""
            comments = node.get("comments", {}).get("nodes", [])
            if comments:
                first_comment = comments[0].get("body", "")
            threads.append(
                ReviewThread(
                    id=node["id"],
                    is_resolved=False,
                    is_outdated=False,
                    body=first_comment,
                )
            )
        return threads
    except Exception:
        logger.warning("Failed to fetch review threads for %s#%d", repo, pr_number, exc_info=True)
        return []


# ── PR merge / ready ──────────────────────────────────────


async def merge_pr(repo: str, pr_number: int) -> bool:
    """Squash-merge a PR. Returns True on success, False on any failure. Never raises."""
    try:
        async with _client() as client:
            resp = await client.put(
                f"{_API}/repos/{repo}/pulls/{pr_number}/merge",
                json={"merge_method": "squash"},
            )
        if resp.status_code == 200:
            return True
        logger.warning(
            "Merge failed for %s#%d: HTTP %d — %s",
            repo,
            pr_number,
            resp.status_code,
            resp.text,
        )
        return False
    except Exception:
        logger.warning("Merge API call failed for %s#%d", repo, pr_number, exc_info=True)
        return False


async def lock_pr(repo: str, pr_number: int) -> None:
    """Lock a PR conversation to prevent non-collaborator comments.

    Best-effort — logs on failure, never raises.
    Mitigates prompt injection via external comments on agent PRs.
    The bot retains full comment access via the App installation token.
    """
    try:
        async with _client() as client:
            resp = await client.put(
                f"{_API}/repos/{repo}/issues/{pr_number}/lock",
                json={"lock_reason": "resolved"},
            )
        if resp.status_code not in (204, 200):
            logger.warning(
                "Failed to lock PR #%d on %s: HTTP %d", pr_number, repo, resp.status_code
            )
    except Exception:
        logger.warning("Failed to lock PR #%d on %s", pr_number, repo, exc_info=True)


async def mark_pr_ready(repo: str, pr_number: int) -> None:
    """Mark a draft PR as ready for review. Best-effort — logs on failure, never raises."""
    try:
        async with _client() as client:
            resp = await client.patch(
                f"{_API}/repos/{repo}/pulls/{pr_number}",
                json={"draft": False},
            )
            if resp.status_code != 200:
                logger.warning(
                    "Failed to mark PR #%d ready on %s: HTTP %d",
                    pr_number,
                    repo,
                    resp.status_code,
                )
    except Exception:
        logger.warning("Failed to mark PR #%d ready on %s", pr_number, repo, exc_info=True)
