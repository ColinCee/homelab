"""Tests for Copilot CLI stats parsing."""

import asyncio
import signal
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from services.copilot import (
    CLIResult,
    TaskError,
    _parse_session_id,
    _parse_stats,
    _parse_time,
    run_copilot,
)

SAMPLE_OUTPUT = """\
Hello world

Total usage est:        3 Premium requests
API time spent:         12s
Total session time:     15s
Total code changes:     +0 -0
Breakdown by AI model:
 gpt-5.4                  26.4k in, 402 out, 0 cached (Est. 2 Premium requests)
 claude-sonnet-4.6          8.1k in, 150 out, 0 cached (Est. 1 Premium request)
"""

SAMPLE_OUTPUT_LONG_TIMES = """\
Total usage est:        1 Premium request
API time spent:         6m 29s
Total session time:     6m 58s
Total code changes:     +0 -0
Breakdown by AI model:
 gpt-5.4                  883.6k in, 17.7k out, 788.5k cached (Est. 1 Premium request)
"""


def test_parse_premium_requests():
    stats = _parse_stats(SAMPLE_OUTPUT)
    assert stats["premium_requests"] == 3


def test_parse_timing():
    stats = _parse_stats(SAMPLE_OUTPUT)
    assert stats["api_time"] == 12
    assert stats["session_time"] == 15


def test_parse_models():
    stats = _parse_stats(SAMPLE_OUTPUT)
    assert "gpt-5.4" in stats["models"]
    assert "claude-sonnet-4.6" in stats["models"]
    assert "26.4k in" in stats["models"]["gpt-5.4"]


def test_parse_empty_output():
    stats = _parse_stats("just some text\nno stats here")
    assert stats["premium_requests"] == 0
    assert stats["models"] == {}


def test_parse_time_minutes_and_seconds():
    assert _parse_time("6m 29s") == 389


def test_parse_time_seconds_only():
    assert _parse_time("45s") == 45


def test_parse_time_minutes_only():
    assert _parse_time("3m") == 180


def test_parse_timing_long_format():
    stats = _parse_stats(SAMPLE_OUTPUT_LONG_TIMES)
    assert stats["api_time"] == 389
    assert stats["session_time"] == 418
    assert stats["premium_requests"] == 1
    assert "gpt-5.4" in stats["models"]


SAMPLE_OUTPUT_MILLIONS = """\
Total usage est:        1 Premium request
API time spent:         9m 15s
Total session time:     6m 43s
Breakdown by AI model:
 gpt-5.4                  2.2m in, 28.9k out, 2.1m cached (Est. 1 Premium request)
"""


def test_parse_models_with_millions():
    stats = _parse_stats(SAMPLE_OUTPUT_MILLIONS)
    assert "gpt-5.4" in stats["models"]
    assert "2.2m in" in stats["models"]["gpt-5.4"]


SAMPLE_OUTPUT_NEW_FORMAT = """\
Changes   +488 -118
Requests  1 Premium (15m 44s)
Tokens    ↑ 5.5m • ↓ 34.0k • 5.3m (cached) • 19.9k (reasoning)
"""


def test_parse_new_cli_format():
    stats = _parse_stats(SAMPLE_OUTPUT_NEW_FORMAT)
    assert stats["premium_requests"] == 1
    assert stats["session_time"] == 15 * 60 + 44
    assert stats["tokens_line"] == "↑ 5.5m • ↓ 34.0k • 5.3m (cached) • 19.9k (reasoning)"
    assert stats["models"] == {}


def test_parse_new_format_short_time():
    stats = _parse_stats("Requests  3 Premium (6m 16s)\n")
    assert stats["premium_requests"] == 3
    assert stats["session_time"] == 6 * 60 + 16


def test_stats_line_strips_est_premium():
    r = CLIResult(
        output="",
        total_premium_requests=1,
        session_time_seconds=201,
        models={"gpt-5.4": "2.2m in, 28.9k out, 2.1m cached (Est. 1 Premium request)"},
    )
    assert "(Est." not in r.stats_line
    assert "2.2m in, 28.9k out, 2.1m cached" in r.stats_line


def test_parse_session_id_from_output():
    output = "Starting session...\nSession ID: `a1b2c3d4-e5f6-7890-abcd-ef1234567890`\nDone."
    assert _parse_session_id(output) == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


def test_parse_session_id_without_backticks():
    output = "Session ID: a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    assert _parse_session_id(output) == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


def test_parse_session_id_returns_none_when_absent():
    assert _parse_session_id("no session info here") is None


def test_parse_session_id_from_markdown_transcript():
    """The --share transcript uses markdown: > - **Session ID:** `<uuid>`"""
    output = "> - **Session ID:** `a1b2c3d4-e5f6-7890-abcd-ef1234567890`"
    assert _parse_session_id(output) == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


class TestRedactSecrets:
    def test_redacts_gh_token(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GH_TOKEN", "ghs_s3cr3t_token_value")
        from services.copilot import _redact_secrets

        assert _redact_secrets("token is ghs_s3cr3t_token_value here") == "token is [REDACTED] here"

    def test_leaves_text_unchanged_without_secrets(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("GH_TOKEN", raising=False)
        from services.copilot import _redact_secrets

        assert _redact_secrets("normal output") == "normal output"

    def test_redacts_extra_secrets(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("GH_TOKEN", raising=False)
        from services.copilot import _redact_secrets

        extras = frozenset({"ghs_injected_token"})
        result = _redact_secrets("error: ghs_injected_token leaked", extras)
        assert result == "error: [REDACTED] leaked"


class TestRunCopilot:
    def _make_mock_process(self, stdout_text: str = "", returncode: int = 0):
        """Create a mock subprocess that yields stdout_text line by line."""
        proc = AsyncMock()
        proc.returncode = returncode

        stdout_lines = (stdout_text + "\n").encode().split(b"\n") if stdout_text else [b""]
        stdout_stream = AsyncMock()
        stdout_stream.at_eof = lambda: len(stdout_lines) == 0

        async def read_stdout():
            if stdout_lines:
                return stdout_lines.pop(0) + b"\n"
            return b""

        stdout_stream.readline = read_stdout

        stderr_stream = AsyncMock()
        stderr_stream.at_eof = lambda: True
        stderr_stream.readline = AsyncMock(return_value=b"")

        proc.stdout = stdout_stream
        proc.stderr = stderr_stream
        proc.wait = AsyncMock()
        return proc

    @patch("services.copilot.asyncio.create_subprocess_exec")
    def test_share_flag_in_command(self, mock_exec: AsyncMock, tmp_path: Path):
        """Verifies --share flag is passed to the CLI."""
        proc = self._make_mock_process("done")
        mock_exec.return_value = proc

        asyncio.run(run_copilot(tmp_path, "test prompt"))

        cmd = mock_exec.call_args[0]
        share_args = [a for a in cmd if a.startswith("--share=")]
        assert len(share_args) == 1
        assert str(tmp_path / ".copilot-session.md") in share_args[0]
        assert mock_exec.call_args.kwargs["start_new_session"] is True

    @patch("services.copilot.asyncio.create_subprocess_exec")
    def test_reads_transcript_when_present(self, mock_exec: AsyncMock, tmp_path: Path):
        """Verifies session transcript is read from the --share output file."""
        transcript_content = "# Session\n\n## Turn 1\n\nUser: test prompt\n"
        transcript_path = tmp_path / ".copilot-session.md"
        transcript_path.write_text(transcript_content)

        proc = self._make_mock_process("done")
        mock_exec.return_value = proc

        result = asyncio.run(run_copilot(tmp_path, "test prompt"))

        assert result.session_transcript == transcript_content

    @patch("services.copilot.asyncio.create_subprocess_exec")
    def test_transcript_none_when_file_missing(self, mock_exec: AsyncMock, tmp_path: Path):
        """Transcript is None when --share file doesn't exist."""
        proc = self._make_mock_process("done")
        mock_exec.return_value = proc

        result = asyncio.run(run_copilot(tmp_path, "test prompt"))

        assert result.session_transcript is None

    @patch("services.copilot.asyncio.create_subprocess_exec")
    def test_resume_flag_in_command(self, mock_exec: AsyncMock, tmp_path: Path):
        """When session_id is provided, --resume flag is passed to the CLI."""
        proc = self._make_mock_process("done")
        mock_exec.return_value = proc

        asyncio.run(run_copilot(tmp_path, "fix prompt", session_id="abc-123-def-456"))

        cmd = mock_exec.call_args[0]
        resume_args = [a for a in cmd if a.startswith("--resume=")]
        assert resume_args == ["--resume=abc-123-def-456"]

    @patch("services.copilot.asyncio.create_subprocess_exec")
    def test_no_resume_flag_without_session_id(self, mock_exec: AsyncMock, tmp_path: Path):
        """Without session_id, no --resume flag is passed."""
        proc = self._make_mock_process("done")
        mock_exec.return_value = proc

        asyncio.run(run_copilot(tmp_path, "test prompt"))

        cmd = mock_exec.call_args[0]
        resume_args = [a for a in cmd if a.startswith("--resume=")]
        assert resume_args == []

    @patch("services.copilot.asyncio.create_subprocess_exec")
    def test_session_id_parsed_from_output(self, mock_exec: AsyncMock, tmp_path: Path):
        """Session ID is extracted from CLI stdout."""
        proc = self._make_mock_process(
            "Starting...\nSession ID: `a1b2c3d4-e5f6-7890-abcd-ef1234567890`\nDone."
        )
        mock_exec.return_value = proc

        result = asyncio.run(run_copilot(tmp_path, "test prompt"))

        assert result.session_id == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    @patch("services.copilot.os.killpg")
    @patch(
        "services.copilot.asyncio.wait_for",
        new_callable=AsyncMock,
        side_effect=asyncio.CancelledError,
    )
    @patch("services.copilot.asyncio.create_subprocess_exec")
    def test_kills_process_when_cancelled(
        self,
        mock_exec: AsyncMock,
        _mock_wait_for: AsyncMock,
        mock_killpg: Mock,
        tmp_path: Path,
    ):
        proc = AsyncMock()
        proc.returncode = 0
        proc.kill = Mock()
        proc.pid = 12345

        stdout_stream = AsyncMock()
        stdout_stream.at_eof = lambda: True
        stdout_stream.readline = AsyncMock(return_value=b"")

        stderr_stream = AsyncMock()
        stderr_stream.at_eof = lambda: True
        stderr_stream.readline = AsyncMock(return_value=b"")

        proc.stdout = stdout_stream
        proc.stderr = stderr_stream
        proc.wait = AsyncMock()
        mock_exec.return_value = proc

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run_copilot(tmp_path, "test prompt"))

        mock_killpg.assert_called_once_with(12345, signal.SIGKILL)
        proc.kill.assert_not_called()
        proc.wait.assert_awaited_once()

    @patch("services.copilot.os.killpg")
    @patch("services.copilot.asyncio.wait_for", new_callable=AsyncMock, side_effect=TimeoutError)
    @patch("services.copilot.asyncio.create_subprocess_exec")
    def test_kills_process_group_when_timed_out(
        self,
        mock_exec: AsyncMock,
        _mock_wait_for: AsyncMock,
        mock_killpg: Mock,
        tmp_path: Path,
    ):
        proc = AsyncMock()
        proc.returncode = 0
        proc.kill = Mock()
        proc.pid = 67890

        stdout_stream = AsyncMock()
        stdout_stream.at_eof = lambda: True
        stdout_stream.readline = AsyncMock(return_value=b"")

        stderr_stream = AsyncMock()
        stderr_stream.at_eof = lambda: True
        stderr_stream.readline = AsyncMock(return_value=b"")

        proc.stdout = stdout_stream
        proc.stderr = stderr_stream
        proc.wait = AsyncMock()
        mock_exec.return_value = proc

        with pytest.raises(TaskError, match="timed out"):
            asyncio.run(run_copilot(tmp_path, "test prompt"))

        mock_killpg.assert_called_once_with(67890, signal.SIGKILL)
        proc.kill.assert_not_called()
        proc.wait.assert_awaited_once()
