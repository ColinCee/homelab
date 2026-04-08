"""Copilot CLI headless runner — invokes the copilot binary for code review."""

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

COPILOT_BINARY = "/usr/local/bin/copilot"
TIMEOUT_SECONDS = 600


async def run_copilot(
    worktree_path: Path,
    prompt: str,
    *,
    model: str = "gpt-5.4",
    effort: str = "high",
    gh_token: str | None = None,
) -> str:
    """Run Copilot CLI in headless mode.

    The CLI runs inside the worktree directory so it can access the full
    codebase, read .github/copilot-instructions.md, use tools (grep,
    view, gh, etc.) to understand context and post reviews.
    """
    cmd = [
        COPILOT_BINARY,
        "-p",
        prompt,
        "--model",
        model,
        "--effort",
        effort,
        "--yolo",
        "--no-ask-user",
    ]

    env = os.environ.copy()
    if gh_token:
        env["GH_TOKEN"] = gh_token

    logger.info("Running Copilot CLI in %s (model=%s, effort=%s)", worktree_path, model, effort)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=worktree_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
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
    logger.info("Copilot CLI finished (%d bytes output)", len(output))
    logger.debug("Copilot CLI output:\n%s", output)
    return output
