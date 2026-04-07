"""Copilot CLI headless runner — invokes the copilot binary for code review."""

import asyncio
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

COPILOT_BINARY = "/usr/local/bin/copilot"
TIMEOUT_SECONDS = 600


async def run_review(
    worktree_path: Path,
    prompt: str,
    *,
    model: str = "gpt-5.4",
    effort: str = "high",
) -> str:
    """Run Copilot CLI in headless mode and return the response text.

    The CLI runs inside the worktree directory so it can access the full
    codebase, read .github/copilot-instructions.md, and use tools (grep,
    view, etc.) to understand context.
    """
    cmd = [
        COPILOT_BINARY,
        "-p",
        prompt,
        "--model",
        model,
        "--effort",
        effort,
        "-s",
        "--yolo",
        "--no-ask-user",
    ]

    logger.info("Running Copilot CLI in %s (model=%s, effort=%s)", worktree_path, model, effort)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=worktree_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=TIMEOUT_SECONDS,
        )
    except TimeoutError as err:
        proc.kill()
        raise RuntimeError(f"Copilot CLI timed out after {TIMEOUT_SECONDS}s") from err

    if proc.returncode != 0:
        error = stderr.decode()
        logger.error("Copilot CLI failed (exit %d): %s", proc.returncode, error)
        raise RuntimeError(f"Copilot CLI exited with code {proc.returncode}: {error}")

    output = stdout.decode()
    logger.debug("Copilot CLI raw output (%d bytes)", len(output))
    return output


def extract_json(text: str) -> dict:
    """Extract a JSON object from the Copilot CLI response.

    Handles both raw JSON and JSON wrapped in markdown code fences.
    """
    # Try parsing as raw JSON first
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    # Look for JSON in code fences
    fence_match = re.search(r"```(?:json)?\s*\n({.*?})\s*\n```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Last resort: find the first { ... } block
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not extract JSON from Copilot CLI output:\n{text[:500]}")
