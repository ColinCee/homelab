"""Copilot CLI headless runner — invokes the copilot binary for code review."""

import asyncio
import contextlib
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
            # Strip redundant "(Est. N Premium request(s))" — shown separately
            clean = re.sub(r"\s*\(Est\..*?\)", "", detail).strip().rstrip(",")
            parts.append(f"🤖 {model_name} ({clean})")
        if self.total_premium_requests:
            parts.append(f"💰 {self.total_premium_requests} premium request(s)")
        if self.session_time_seconds:
            parts.append(f"⏱️ {self.session_time_seconds}s")
        return " · ".join(parts) if parts else ""


def _parse_time(value: str) -> int:
    """Parse a time string like '6m 29s', '45s', or '3m' into total seconds."""
    total = 0
    if m := re.search(r"(\d+)m", value):
        total += int(m.group(1)) * 60
    if m := re.search(r"(\d+)s", value):
        total += int(m.group(1))
    return total


def _parse_stats(output: str) -> dict:
    """Parse CLI session stats from non-silent output."""
    stats: dict = {"premium_requests": 0, "api_time": 0, "session_time": 0, "models": {}}

    for line in output.splitlines():
        line = line.strip()

        if m := re.match(r"Total usage est:\s+(\d+)\s+Premium", line):
            stats["premium_requests"] = int(m.group(1))
        elif m := re.match(r"API time spent:\s+(.+)", line):
            stats["api_time"] = _parse_time(m.group(1))
        elif m := re.match(r"Total session time:\s+(.+)", line):
            stats["session_time"] = _parse_time(m.group(1))
        elif m := re.match(r"^\s*(\S+)\s+([\d.]+[km]?\s+in,\s+[\d.]+[km]?\s+out.*)", line):
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

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    async def _stream(stream: asyncio.StreamReader, lines: list[str], prefix: str) -> None:
        while not stream.at_eof():
            try:
                raw = await asyncio.wait_for(stream.readline(), timeout=5.0)
                if raw:
                    line = raw.decode().rstrip()
                    lines.append(line)
                    logger.info("[copilot %s] %s", prefix, line)
            except TimeoutError:
                if proc.returncode is not None:
                    break

    try:
        assert proc.stdout and proc.stderr
        out_task = asyncio.create_task(_stream(proc.stdout, stdout_lines, "agent"))
        err_task = asyncio.create_task(_stream(proc.stderr, stderr_lines, "meta"))

        await asyncio.wait_for(proc.wait(), timeout=TIMEOUT_SECONDS)
        # Give streams a moment to flush, then cancel
        await asyncio.sleep(1)
        out_task.cancel()
        err_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(out_task, err_task)
    except TimeoutError as err:
        proc.kill()
        raise RuntimeError(f"Copilot CLI timed out after {TIMEOUT_SECONDS}s") from err

    if proc.returncode != 0:
        error = "\n".join(stderr_lines) or "\n".join(stdout_lines)
        logger.error("Copilot CLI failed (exit %d): %s", proc.returncode, error)
        raise RuntimeError(f"Copilot CLI exited with code {proc.returncode}: {error}")

    output = "\n".join(stdout_lines)
    all_output = "\n".join(stdout_lines + stderr_lines)
    logger.info("Copilot CLI finished (%d bytes output)", len(output))

    stats = _parse_stats(all_output)
    return CLIResult(
        output=output,
        total_premium_requests=stats["premium_requests"],
        api_time_seconds=stats["api_time"],
        session_time_seconds=stats["session_time"],
        models=stats["models"],
    )
