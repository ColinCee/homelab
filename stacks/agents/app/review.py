"""PR review logic — fetch diff, call Copilot, post comment."""

import logging
import time

import httpx

from copilot import chat, get_token

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a senior engineer reviewing a pull request. Review the diff for:

- **Bugs**: Logic errors, off-by-one, null/undefined issues, race conditions
- **Security**: Injection, secrets in code, unsafe deserialization, path traversal
- **Breaking changes**: API contract changes, config format changes
- **Missing edge cases**: Error handling, empty inputs, boundary conditions

## Rules

- Only comment on things that genuinely matter
- Never comment on style, formatting, naming conventions, or trivial issues
- If the PR looks good, say so briefly — don't invent problems
- Group related issues together rather than commenting line-by-line
- Be specific: quote the problematic code and explain why it's wrong
- Suggest a fix when possible
- Keep the review concise and actionable
"""

MAX_DIFF_BYTES = 80_000


def _format_tokens(n: int) -> str:
    """Format token count with comma separators."""
    return f"{n:,}"


# Premium request multipliers per model (source: GitHub Copilot docs)
# 0 = unlimited/included on paid plans, not counted as premium
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


async def fetch_pr_diff(repo: str, pr_number: int) -> tuple[str, str, str]:
    """Fetch PR title, body, and diff from GitHub API."""
    headers = {
        "Authorization": f"Bearer {get_token()}",
        "Accept": "application/vnd.github+json",
    }
    base = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"

    async with httpx.AsyncClient(timeout=30) as client:
        pr_resp = await client.get(base, headers=headers)
        pr_resp.raise_for_status()
        pr_data = pr_resp.json()

        diff_resp = await client.get(
            base, headers={**headers, "Accept": "application/vnd.github.diff"}
        )
        diff_resp.raise_for_status()

    title = pr_data.get("title", "")
    body = pr_data.get("body", "") or ""
    diff = diff_resp.text[:MAX_DIFF_BYTES]

    return title, body, diff


async def review_pr(
    *,
    repo: str,
    pr_number: int,
    model: str = "gpt-5.4",
    reasoning_effort: str = "high",
) -> str:
    """Run a full review: fetch diff → Copilot → post comment."""
    logger.info("Reviewing %s#%d with %s (reasoning: %s)", repo, pr_number, model, reasoning_effort)

    title, body, diff = await fetch_pr_diff(repo, pr_number)
    user_content = f"## PR: {title}\n\n{body}\n\n## Diff\n\n{diff}"

    start = time.monotonic()
    result = await chat(
        system=SYSTEM_PROMPT,
        user=user_content,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    elapsed = time.monotonic() - start

    # Build metadata footer
    prompt = _format_tokens(result.prompt_tokens)
    completion = _format_tokens(result.completion_tokens)
    total = _format_tokens(result.total_tokens)
    stats = [
        f"⏱️ {elapsed:.1f}s",
        f"📊 {total} tokens ({prompt} prompt → {completion} completion)",
    ]
    if result.reasoning_tokens > 0:
        stats.append(f"🧠 {_format_tokens(result.reasoning_tokens)} reasoning tokens")
    if result.cached_tokens > 0:
        stats.append(f"💾 {_format_tokens(result.cached_tokens)} cached tokens")
    stats.append(f"⚡ reasoning: {reasoning_effort}")

    # Premium request cost
    multiplier = MODEL_MULTIPLIERS.get(result.model, 1)
    if multiplier > 0:
        cost = multiplier * OVERAGE_COST_PER_REQUEST
        stats.append(f"💰 {multiplier}x premium request (${cost:.2f})")
    else:
        stats.append("✅ included (0 premium requests)")

    footer = f"\n\n---\n🤖 *Reviewed by {result.model}* · {' · '.join(stats)}"
    comment_body = result.content + footer

    comment_url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            comment_url,
            headers={
                "Authorization": f"Bearer {get_token()}",
                "Accept": "application/vnd.github+json",
            },
            json={"body": comment_body},
        )
        resp.raise_for_status()

    logger.info(
        "Review posted on %s#%d — %d words, %d tokens, %.1fs",
        repo,
        pr_number,
        len(result.content.split()),
        result.total_tokens,
        elapsed,
    )
    return comment_body
