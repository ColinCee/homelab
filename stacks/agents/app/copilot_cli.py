"""Copilot CLI headless runner — invokes the copilot binary for code review."""

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

COPILOT_BINARY = "/usr/local/bin/copilot"
TIMEOUT_SECONDS = 600


@dataclass
class CLIResult:
    """Result from a Copilot CLI invocation."""

    output: str
    total_premium_requests: int = 0
    api_time_seconds: int = 0
    session_time_seconds: int = 0
    models: dict[str, str] = field(default_factory=dict)

    @property
    def stats_line(self) -> str:
        """One-line stats summary for review footer."""
        parts = []
        for model_name, detail in self.models.items():
            parts.append(f"🤖 {model_name} ({detail})")
        if self.total_premium_requests:
            parts.append(f"💰 {self.total_premium_requests} premium request(s)")
        if self.session_time_seconds:
            parts.append(f"⏱️ {self.session_time_seconds}s")
        return " · ".join(parts) if parts else ""


def _parse_stats(output: str) -> dict:
    """Parse CLI session stats from non-silent output."""
    stats: dict = {"premium_requests": 0, "api_time": 0, "session_time": 0, "models": {}}

    for line in output.splitlines():
        line = line.strip()

        if m := re.match(r"Total usage est:\s+(\d+)\s+Premium", line):
            stats["premium_requests"] = int(m.group(1))
        elif m := re.match(r"API time spent:\s+(\d+)s", line):
            stats["api_time"] = int(m.group(1))
        elif m := re.match(r"Total session time:\s+(\d+)s", line):
            stats["session_time"] = int(m.group(1))
        elif m := re.match(r"^\s*(\S+)\s+([\d.]+k?\s+in,\s+[\d.]+k?\s+out.*)", line):
            model_name = m.group(1)
            if model_name not in ("Total", "Breakdown"):
                stats["models"][model_name] = m.group(2).strip()

    return stats


async def run_copilot(
    worktree_path: Path,
    prompt: str,
    *,
    model: str = "gpt-5.4",
    effort: str = "high",
    gh_token: str | None = None,
) -> CLIResult:
    """Run Copilot CLI in headless mode and return result with stats."""
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
        "--autopilot",
    ]

    env = os.environ.copy()
    if gh_token:
        env["GH_TOKEN"] = gh_token

    logger.info("Running Copilot CLI in %s (model=%s, effort=%s)", worktree_path, model, effort)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=worktree_path,
        stdin=asyncio.subprocess.DEVNULL,
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
        error = stderr.decode() or stdout.decode()
        logger.error("Copilot CLI failed (exit %d): %s", proc.returncode, error)
        raise RuntimeError(f"Copilot CLI exited with code {proc.returncode}: {error}")

    output = stdout.decode()
    logger.info("Copilot CLI finished (%d bytes output)", len(output))

    stats = _parse_stats(output)
    return CLIResult(
        output=output,
        total_premium_requests=stats["premium_requests"],
        api_time_seconds=stats["api_time"],
        session_time_seconds=stats["session_time"],
        models=stats["models"],
    )
