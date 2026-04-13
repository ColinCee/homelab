"""Copilot CLI headless runner — invokes the copilot binary for agent tasks."""

import asyncio
import contextlib
import logging
import os
import re
import signal
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

COPILOT_BINARY = "/usr/local/bin/copilot"
# Last-resort safety net — prevents a truly hung process from sitting forever.
# This should never fire during normal operation; productive runs complete well under this.
TIMEOUT_SECONDS = 1800

# Tokens to redact from CLI output before logging or including in errors.
_redact_env_keys = ("GH_TOKEN", "COPILOT_GITHUB_TOKEN", "GITHUB_TOKEN")

# Allowlisted env vars for CLI subprocess — keeps server secrets
# (GITHUB_APP_*, orchestration tokens) out of the autonomous CLI.
_CLI_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "COPILOT_GITHUB_TOKEN",
        "MISE_DATA_DIR",
        "MISE_CONFIG_DIR",
        "MISE_CACHE_DIR",
    }
)


def _redact_secrets(text: str, extra_secrets: frozenset[str] = frozenset()) -> str:
    """Replace known secret values with [REDACTED] in text."""
    result = text
    for key in _redact_env_keys:
        val = os.environ.get(key)
        if val and val in result:
            result = result.replace(val, "[REDACTED]")
    for secret in extra_secrets:
        if secret in result:
            result = result.replace(secret, "[REDACTED]")
    return result


class TaskError(Exception):
    """Wraps a post-CLI failure, preserving the premium request count for metrics."""

    def __init__(self, message: str, *, premium_requests: int = 0, commented: bool = False) -> None:
        super().__init__(message)
        self.premium_requests = premium_requests
        # True when an error comment was already posted on the PR/issue,
        # so callers can avoid double-posting.
        self.commented = commented


@dataclass
class CLIResult:
    """Result from a Copilot CLI invocation."""

    output: str
    total_premium_requests: int = 0
    api_time_seconds: int = 0
    session_time_seconds: int = 0
    models: dict[str, str] = field(default_factory=dict)
    tokens_line: str = ""
    session_transcript: str | None = None
    session_id: str | None = None

    @property
    def stats_line(self) -> str:
        """One-line stats summary for review footer."""
        parts = []
        for model_name, detail in self.models.items():
            # Strip redundant "(Est. N Premium request(s))" — shown separately
            clean = re.sub(r"\s*\(Est\..*?\)", "", detail).strip().rstrip(",")
            parts.append(f"🤖 {model_name} ({clean})")
        if self.tokens_line:
            parts.append(f"📊 {self.tokens_line}")
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
    """Parse CLI session stats from non-silent output.

    Handles both old and new CLI formats:
      Old: Total usage est: 3 Premium requests / API time spent: 6m 29s / ...
      New: Requests 1 Premium (15m 44s) / Tokens ↑ 5.5m • ↓ 34.0k • ...
    """
    stats: dict = {"premium_requests": 0, "api_time": 0, "session_time": 0, "models": {}}

    for line in output.splitlines():
        line = line.strip()

        # Premium requests — old: "Total usage est: 3 Premium"
        #                    new: "Requests 1 Premium (15m 44s)"
        if m := re.match(r"(?:Total usage est:|Requests)\s+(\d+)\s+Premium", line):
            stats["premium_requests"] = int(m.group(1))
            # New format embeds session time in parentheses on the same line
            if t := re.search(r"\((\d+m\s*\d*s?)\)", line):
                stats["session_time"] = _parse_time(t.group(1))

        # Old format only
        elif m := re.match(r"API time spent:\s+(.+)", line):
            stats["api_time"] = _parse_time(m.group(1))
        elif m := re.match(r"Total session time:\s+(.+)", line):
            stats["session_time"] = _parse_time(m.group(1))

        # Tokens — new: "Tokens ↑ 5.5m • ↓ 34.0k • 5.3m (cached) • 19.9k (reasoning)"
        elif m := re.match(r"Tokens\s+(.+)", line):
            stats["tokens_line"] = m.group(1).strip()

        # Model usage — old: "gpt-5.4  5.5m in, 34.0k out ..."
        elif m := re.match(r"^\s*(\S+)\s+([\d.]+[km]?\s+in,\s+[\d.]+[km]?\s+out.*)", line):
            model_name = m.group(1)
            if model_name not in ("Total", "Breakdown"):
                stats["models"][model_name] = m.group(2).strip()

    return stats


SESSION_TRANSCRIPT_FILE = ".copilot-session.md"

# Matches both plain stdout "Session ID: <uuid>" and the markdown
# transcript format "> - **Session ID:** `<uuid>`"
_SESSION_ID_RE = re.compile(r"Session ID:?\*{0,2}\s*`?([0-9a-f-]{36})`?")


def _parse_session_id(text: str) -> str | None:
    """Extract session ID UUID from CLI output or transcript."""
    if m := _SESSION_ID_RE.search(text):
        return m.group(1)
    return None


async def run_copilot(
    worktree_path: Path,
    prompt: str,
    *,
    model: str = "gpt-5.4",
    effort: str = "high",
    session_id: str | None = None,
    github_token: str | None = None,
) -> CLIResult:
    """Run Copilot CLI in headless mode and return result with stats.

    When github_token is provided, GH_TOKEN is set in the CLI environment,
    giving it full repo access (push, PR creation, reviews, merge).
    """
    transcript_path = worktree_path / SESSION_TRANSCRIPT_FILE
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
        f"--share={transcript_path}",
    ]

    if session_id:
        cmd.append(f"--resume={session_id}")

    env = {k: v for k, v in os.environ.items() if k in _CLI_ENV_ALLOWLIST}
    secrets: frozenset[str] = frozenset()
    if github_token:
        env["GH_TOKEN"] = github_token
        secrets = frozenset({github_token})
    else:
        env.pop("GH_TOKEN", None)

    logger.info(
        "Running Copilot CLI in %s (model=%s, effort=%s, resume=%s)",
        worktree_path,
        model,
        effort,
        session_id or "none",
    )

    if os.name == "posix":
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=worktree_path,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    else:
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
    out_task: asyncio.Task[None] | None = None
    err_task: asyncio.Task[None] | None = None
    stream_tasks: asyncio.Future[tuple[None, None]] | None = None

    async def _stream(stream: asyncio.StreamReader, lines: list[str], prefix: str) -> None:
        while not stream.at_eof():
            raw = await stream.readline()
            if raw:
                line = _redact_secrets(raw.decode().rstrip(), secrets)
                lines.append(line)
                logger.info("[copilot %s] %s", prefix, line)

    async def _stop_process() -> None:
        if os.name == "posix" and proc.pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        else:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        for task in (out_task, err_task):
            if task is not None:
                task.cancel()
        if stream_tasks is not None:
            stream_tasks.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            if stream_tasks is not None:
                await stream_tasks
            else:
                await asyncio.gather(*(task for task in (out_task, err_task) if task is not None))
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()

    try:
        assert proc.stdout and proc.stderr
        out_task = asyncio.create_task(_stream(proc.stdout, stdout_lines, "agent"))
        err_task = asyncio.create_task(_stream(proc.stderr, stderr_lines, "meta"))
        stream_tasks = asyncio.gather(out_task, err_task)

        # Wait for streams to EOF (happens when the process exits and pipes close).
        # The overall timeout covers both the process runtime and stream draining.
        await asyncio.wait_for(stream_tasks, timeout=TIMEOUT_SECONDS)
        await proc.wait()
    except asyncio.CancelledError:
        await _stop_process()
        raise
    except TimeoutError as err:
        await _stop_process()
        stats = _parse_stats("\n".join(stdout_lines + stderr_lines))
        raise TaskError(
            f"Copilot CLI timed out after {TIMEOUT_SECONDS}s",
            premium_requests=stats["premium_requests"],
        ) from err

    if proc.returncode != 0:
        error = _redact_secrets("\n".join(stderr_lines) or "\n".join(stdout_lines), secrets)
        logger.error("Copilot CLI failed (exit %d): %s", proc.returncode, error)
        stats = _parse_stats("\n".join(stdout_lines + stderr_lines))
        raise TaskError(
            f"Copilot CLI exited with code {proc.returncode}: {error}",
            premium_requests=stats["premium_requests"],
        )

    output = "\n".join(stdout_lines)
    all_output = "\n".join(stdout_lines + stderr_lines)
    logger.info("Copilot CLI finished (%d bytes output)", len(output))

    transcript = None
    if transcript_path.exists():
        transcript = transcript_path.read_text()
        logger.info("Session transcript captured (%d bytes)", len(transcript))
        logger.debug("Session transcript:\n%s", transcript)
    else:
        logger.warning("No session transcript found at %s", transcript_path)

    stats = _parse_stats(all_output)
    parsed_session_id = _parse_session_id(all_output)
    if not parsed_session_id and transcript:
        parsed_session_id = _parse_session_id(transcript)

    return CLIResult(
        output=output,
        total_premium_requests=stats["premium_requests"],
        api_time_seconds=stats["api_time"],
        session_time_seconds=stats["session_time"],
        models=stats["models"],
        tokens_line=stats.get("tokens_line", ""),
        session_transcript=transcript,
        session_id=parsed_session_id,
    )
