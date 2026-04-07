"""PR review logic — fetch diff, call Copilot, return structured review."""

import logging
import time
from enum import StrEnum

import httpx
from pydantic import BaseModel, Field

from copilot import chat, get_token

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a senior engineer reviewing a pull request.

## Philosophy

Your job is to catch problems that would make the codebase worse. Default to \
approving — if the code works correctly and is reasonably clear, approve it \
even if you'd write it differently. Trust the author.

Identify problems. Do NOT suggest fixes — the author will decide how to fix \
them. Focus on WHAT is wrong and WHY, not HOW to fix it.

## What to look for

- **Bugs**: Logic errors, off-by-one, null/undefined, race conditions
- **Security**: Injection, secrets in code, unsafe deserialization, path traversal
- **Breaking changes**: API contract changes, config format changes
- **Missing error handling**: Unhandled exceptions, silent failures

Do NOT comment on: style, formatting, naming conventions, or subjective \
preferences. Linters handle style.

## Severity levels

- `blocker` — Real bugs, security issues, data loss, breaking changes. \
Use sparingly — only for things that are objectively wrong.
- `suggestion` — Non-blocking improvement. Author decides whether to adopt.
- `question` — "Did you consider X?" Seeks clarification, not a demand.

## Verdict rules

- `request_changes` ONLY if there is at least one `blocker` comment
- `approve` if the PR looks good, or only has `suggestion`/`question` items
- `approve` with comments is the normal outcome for decent code with minor issues
- `line` is the line number in the NEW version of the file (right side of diff)
- `start_line` is optional — set for multi-line comments (start_line to line)
- Keep comments concise and actionable
- If the PR looks good, return verdict `approve` with an empty comments array
"""

MAX_DIFF_BYTES = 80_000

# Premium request multipliers per model (source: GitHub Copilot docs)
MODEL_MULTIPLIERS: dict[str, float] = {
    "gpt-5-mini": 0,
    "gpt-4.1": 0,
    "gpt-4o": 0,
    "claude-haiku-4.5": 0.33,
    "o3-mini": 0.33,
    "o4-mini": 0.33,
    "gemini-2.0-flash": 0.25,
    "claude-sonnet-4.6": 1,
    "gpt-5.4": 1,
    "gpt-5.2-codex": 1,
    "gemini-pro-2.5": 1,
    "claude-opus-4.6": 3,
}
OVERAGE_COST_PER_REQUEST = 0.04  # USD

SEVERITY_EMOJI = {
    "blocker": "🚫",
    "suggestion": "💡",
    "question": "❓",
}


# -- Pydantic models for structured output --


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
    """Structured review output — schema sent to the API via response_format."""

    summary: str
    verdict: Verdict
    comments: list[LLMComment] = Field(default_factory=list)


# -- Internal result types --


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

        # Build metadata footer
        meta = self.metadata
        stats_parts = []
        if "elapsed_seconds" in meta:
            stats_parts.append(f"⏱️ {meta['elapsed_seconds']:.1f}s")
        if "total_tokens" in meta:
            prompt = f"{meta.get('prompt_tokens', 0):,}"
            completion = f"{meta.get('completion_tokens', 0):,}"
            total = f"{meta['total_tokens']:,}"
            stats_parts.append(f"📊 {total} tokens ({prompt} prompt → {completion} completion)")
        if meta.get("reasoning_tokens", 0) > 0:
            stats_parts.append(f"🧠 {meta['reasoning_tokens']:,} reasoning tokens")
        if meta.get("cached_tokens", 0) > 0:
            stats_parts.append(f"💾 {meta['cached_tokens']:,} cached tokens")
        if "reasoning_effort" in meta:
            stats_parts.append(f"⚡ reasoning: {meta['reasoning_effort']}")
        if "premium_multiplier" in meta:
            m = meta["premium_multiplier"]
            if m > 0:
                cost = m * OVERAGE_COST_PER_REQUEST
                stats_parts.append(f"💰 {m}x premium (${cost:.2f}/req overage)")
            else:
                stats_parts.append("💰 0x (included)")

        stats_line = " · ".join(stats_parts)
        body = f"{self.summary}\n\n---\n🤖 *Reviewed by {meta.get('model', 'unknown')}*"
        if stats_line:
            body += f"\n{stats_line}"

        return {
            "event": event_map.get(self.verdict, "APPROVE"),
            "body": body,
            "comments": gh_comments,
        }

    def to_dict(self) -> dict:
        """Serialize for JSON API response."""
        result = self.to_github_review()
        result["raw"] = {
            "summary": self.summary,
            "verdict": self.verdict.value,
            "comments": [c.model_dump() for c in self.comments],
            "metadata": self.metadata,
        }
        return result


# -- GitHub API helpers --


async def _github_get(client: httpx.AsyncClient, url: str, headers: dict) -> httpx.Response:
    """GET with GitHub API headers."""
    resp = await client.get(url, headers=headers)
    resp.raise_for_status()
    return resp


async def fetch_pr_diff(repo: str, pr_number: int) -> tuple[str, str, str]:
    """Fetch PR title, body, and diff from GitHub API."""
    headers = {
        "Authorization": f"Bearer {get_token()}",
        "Accept": "application/vnd.github+json",
    }
    base = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"

    async with httpx.AsyncClient(timeout=30) as client:
        pr_resp = await _github_get(client, base, headers)
        pr_data = pr_resp.json()

        diff_resp = await client.get(
            base,
            headers={**headers, "Accept": "application/vnd.github.diff"},
        )
        diff_resp.raise_for_status()

    title = pr_data.get("title", "")
    body = pr_data.get("body", "") or ""
    diff = diff_resp.text[:MAX_DIFF_BYTES]

    return title, body, diff


async def fetch_previous_reviews(repo: str, pr_number: int) -> str:
    """Fetch prior bot reviews to give the model context on what it already said."""
    headers = {
        "Authorization": f"Bearer {get_token()}",
        "Accept": "application/vnd.github+json",
    }
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await _github_get(client, url, headers)
        reviews = resp.json()

        # Filter to bot reviews only (github-actions[bot])
        bot_reviews = [
            r for r in reviews if r.get("user", {}).get("login") == "github-actions[bot]"
        ]

        if not bot_reviews:
            return ""

        # Get the latest bot review
        latest = bot_reviews[-1]
        review_id = latest["id"]
        verdict = latest.get("state", "UNKNOWN")

        # Fetch inline comments for this review
        comments_url = (
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews/{review_id}/comments"
        )
        comments_resp = await _github_get(client, comments_url, headers)
        comments = comments_resp.json()

    parts = [f"Previous review verdict: {verdict}"]
    if latest.get("body"):
        # Strip the metadata footer
        body = latest["body"].split("\n---\n")[0].strip()
        if body:
            parts.append(f"Summary: {body}")

    for c in comments:
        path = c.get("path", "?")
        line = c.get("line") or c.get("original_line", "?")
        comment_body = c.get("body", "")
        parts.append(f"- {path}:{line} — {comment_body}")

    return "\n".join(parts)


# -- Main review function --


async def review_pr(
    *,
    repo: str,
    pr_number: int,
    model: str = "gpt-5.4",
    reasoning_effort: str = "high",
) -> ReviewResult:
    """Run a full review: fetch diff → Copilot → return structured result."""
    logger.info(
        "Reviewing %s#%d with %s (reasoning: %s)",
        repo,
        pr_number,
        model,
        reasoning_effort,
    )

    title, body, diff = await fetch_pr_diff(repo, pr_number)
    previous = await fetch_previous_reviews(repo, pr_number)

    user_content = f"## PR: {title}\n\n{body}\n\n## Diff\n\n{diff}"
    if previous:
        user_content += (
            f"\n\n## Your Previous Review\n\n{previous}\n\n"
            "Check whether your previous findings were addressed. "
            "Do NOT repeat findings that have been fixed. "
            "If all previous issues are resolved and no new issues exist, "
            "set verdict to approve."
        )

    start = time.monotonic()
    result = await chat(
        system=SYSTEM_PROMPT,
        user=user_content,
        model=model,
        reasoning_effort=reasoning_effort,
        response_schema=LLMReview,
    )
    elapsed = time.monotonic() - start

    # Parse structured response — guaranteed valid by response_format
    llm_review = LLMReview.model_validate_json(result.content)

    # Enforce verdict consistency
    has_blocker = any(c.severity == Severity.blocker for c in llm_review.comments)
    verdict = llm_review.verdict
    if has_blocker and verdict == Verdict.approve:
        verdict = Verdict.request_changes
    if not has_blocker and verdict == Verdict.request_changes:
        verdict = Verdict.approve

    multiplier = MODEL_MULTIPLIERS.get(result.model, 1)

    review = ReviewResult(
        summary=llm_review.summary,
        verdict=verdict,
        comments=llm_review.comments,
        metadata={
            "model": result.model,
            "elapsed_seconds": elapsed,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
            "reasoning_tokens": result.reasoning_tokens,
            "cached_tokens": result.cached_tokens,
            "reasoning_effort": reasoning_effort,
            "premium_multiplier": multiplier,
        },
    )

    logger.info(
        "Review complete for %s#%d — verdict=%s, %d comments, %d tokens, %.1fs",
        repo,
        pr_number,
        verdict.value,
        len(llm_review.comments),
        result.total_tokens,
        elapsed,
    )
    return review
