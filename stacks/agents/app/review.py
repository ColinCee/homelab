"""PR review logic — orchestrates worktree, Copilot CLI, and GitHub API."""

import logging
import time
from enum import StrEnum

import httpx
from pydantic import BaseModel, Field

from copilot_cli import extract_json, run_review
from github_app import get_installation_token
from worktree import cleanup_worktree, create_worktree

logger = logging.getLogger(__name__)

SEVERITY_EMOJI = {
    "blocker": "🚫",
    "suggestion": "💡",
    "question": "❓",
}

REVIEW_PROMPT_TEMPLATE = """\
Review PR #{pr_number} in {repo}.

**Title:** {title}
**Description:**
{body}

**Changed files:**
{files_list}

{previous_context}

Use the code-review skill. Explore the codebase with grep and view to understand \
how changed code is used. Output a single raw JSON object (no code fences).

**Network note:** You are behind an egress proxy. Only github.com, \
api.githubcopilot.com, githubusercontent.com, and common documentation sites \
(docs.python.org, docs.docker.com, stackoverflow.com, etc.) are reachable. \
Do not attempt to fetch other URLs.
"""


# -- Pydantic models --


class Severity(StrEnum):
    blocker = "blocker"
    suggestion = "suggestion"
    question = "question"


class Verdict(StrEnum):
    approve = "approve"
    request_changes = "request_changes"


class LLMComment(BaseModel):
    """A single review comment from the model."""

    path: str
    line: int
    severity: Severity
    body: str
    start_line: int | None = None


class LLMReview(BaseModel):
    """Structured review output parsed from Copilot CLI response."""

    summary: str
    verdict: Verdict
    comments: list[LLMComment] = Field(default_factory=list)


class ReviewResult(BaseModel):
    """Complete review with metadata, ready for GitHub Reviews API."""

    summary: str
    verdict: Verdict
    comments: list[LLMComment] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)

    def to_github_review(self) -> dict:
        """Convert to GitHub PR Reviews API payload."""
        event_map = {
            Verdict.approve: "APPROVE",
            Verdict.request_changes: "REQUEST_CHANGES",
        }

        gh_comments = []
        for c in self.comments:
            emoji = SEVERITY_EMOJI.get(c.severity.value, "📝")
            body = f"**{emoji} {c.severity.value.replace('_', ' ').title()}**\n\n{c.body}"

            entry: dict = {"path": c.path, "line": c.line, "body": body}
            if c.start_line is not None and c.start_line != c.line:
                entry["start_line"] = c.start_line
            gh_comments.append(entry)

        meta = self.metadata
        stats_parts = []
        if "elapsed_seconds" in meta:
            stats_parts.append(f"⏱️ {meta['elapsed_seconds']:.1f}s")
        if "model" in meta:
            stats_parts.append(f"🤖 {meta['model']}")

        stats_line = " · ".join(stats_parts)
        body = f"{self.summary}\n\n---\n🤖 *Reviewed by homelab-review-bot*"
        if stats_line:
            body += f"\n{stats_line}"

        return {
            "event": event_map.get(self.verdict, "APPROVE"),
            "body": body,
            "comments": gh_comments,
        }


# -- GitHub API helpers --


async def _github_get(client: httpx.AsyncClient, url: str, headers: dict) -> httpx.Response:
    """GET with GitHub API headers."""
    resp = await client.get(url, headers=headers)
    resp.raise_for_status()
    return resp


async def fetch_pr_metadata(repo: str, pr_number: int) -> dict:
    """Fetch PR title, body, and changed files list."""
    token = await get_installation_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    base = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"

    async with httpx.AsyncClient(timeout=30) as client:
        pr_resp = await _github_get(client, base, headers)
        pr_data = pr_resp.json()

        files_resp = await _github_get(client, f"{base}/files", headers)
        files_data = files_resp.json()

    return {
        "title": pr_data.get("title", ""),
        "body": pr_data.get("body", "") or "",
        "files": [f.get("filename", "") for f in files_data],
    }


async def fetch_previous_reviews(repo: str, pr_number: int, bot_login: str) -> str:
    """Fetch prior bot reviews to give the model context on what it already said."""
    token = await get_installation_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await _github_get(client, url, headers)
        reviews = resp.json()

        bot_reviews = [r for r in reviews if r.get("user", {}).get("login") == bot_login]

        if not bot_reviews:
            return ""

        latest = bot_reviews[-1]
        review_id = latest["id"]
        verdict = latest.get("state", "UNKNOWN")

        comments_url = (
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews/{review_id}/comments"
        )
        comments_resp = await _github_get(client, comments_url, headers)
        comments = comments_resp.json()

    parts = [f"Previous review verdict: {verdict}"]
    if latest.get("body"):
        body = latest["body"].split("\n---\n")[0].strip()
        if body:
            parts.append(f"Summary: {body}")

    for c in comments:
        path = c.get("path", "?")
        line = c.get("line") or c.get("original_line", "?")
        comment_body = c.get("body", "")
        parts.append(f"- {path}:{line} — {comment_body}")

    return "\n".join(parts)


async def post_review(repo: str, pr_number: int, review: ReviewResult) -> None:
    """Post a review to GitHub using the App installation token."""
    token = await get_installation_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"
    payload = review.to_github_review()
    payload.pop("event_map", None)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()

    logger.info(
        "Posted review to %s#%d — %s with %d comments",
        repo,
        pr_number,
        payload["event"],
        len(payload.get("comments", [])),
    )


# -- Main review pipeline --


async def review_pr(
    *,
    repo: str,
    pr_number: int,
    model: str = "gpt-5.4",
    reasoning_effort: str = "high",
) -> ReviewResult:
    """Full review pipeline: worktree → Copilot CLI → parse → post → cleanup."""
    logger.info("Starting review for %s#%d (model=%s)", repo, pr_number, model)
    start = time.monotonic()
    repo_url = f"https://github.com/{repo}.git"

    worktree_path = await create_worktree(pr_number, repo_url)

    try:
        pr_meta = await fetch_pr_metadata(repo, pr_number)
        previous = await fetch_previous_reviews(
            repo, pr_number, bot_login="homelab-review-bot[bot]"
        )

        previous_context = ""
        if previous:
            previous_context = (
                f"## Your Previous Review\n\n{previous}\n\n"
                "Check whether your previous findings were addressed. "
                "Do NOT repeat findings that have been fixed. "
                "If all previous issues are resolved and no new issues exist, "
                "set verdict to approve."
            )

        prompt = REVIEW_PROMPT_TEMPLATE.format(
            pr_number=pr_number,
            repo=repo,
            title=pr_meta["title"],
            body=pr_meta["body"],
            files_list="\n".join(f"- {f}" for f in pr_meta["files"]),
            previous_context=previous_context,
        )

        output = await run_review(
            worktree_path,
            prompt,
            model=model,
            effort=reasoning_effort,
        )

        parsed = extract_json(output)
        llm_review = LLMReview.model_validate(parsed)

        # Enforce verdict consistency
        has_blocker = any(c.severity == Severity.blocker for c in llm_review.comments)
        verdict = llm_review.verdict
        if has_blocker and verdict == Verdict.approve:
            verdict = Verdict.request_changes
        if not has_blocker and verdict == Verdict.request_changes:
            verdict = Verdict.approve

        elapsed = time.monotonic() - start

        result = ReviewResult(
            summary=llm_review.summary,
            verdict=verdict,
            comments=llm_review.comments,
            metadata={
                "model": model,
                "elapsed_seconds": elapsed,
                "reasoning_effort": reasoning_effort,
            },
        )

        await post_review(repo, pr_number, result)

        logger.info(
            "Review complete for %s#%d — verdict=%s, %d comments, %.1fs",
            repo,
            pr_number,
            verdict.value,
            len(llm_review.comments),
            elapsed,
        )
        return result

    finally:
        await cleanup_worktree(pr_number)
