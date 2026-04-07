"""PR review logic — fetch diff, call Copilot, post comment."""

import logging

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

    review_text, model_used = await chat(
        system=SYSTEM_PROMPT,
        user=user_content,
        model=model,
        reasoning_effort=reasoning_effort,
    )

    footer = f"\n\n🤖 *Reviewed by {model_used}*"
    comment_body = review_text + footer

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

    logger.info("Review posted on %s#%d (%d words)", repo, pr_number, len(review_text.split()))
    return comment_body
