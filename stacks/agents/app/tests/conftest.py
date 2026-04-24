"""Shared test fixtures and constants."""

from unittest.mock import AsyncMock

import pytest

from auth import require_bearer
from main import app
from models import GitHubIssue
from services.copilot import CLIResult


@pytest.fixture(autouse=True)
def _bypass_bearer_auth():
    """Bypass bearer auth in tests by default. Auth-specific tests opt out."""
    app.dependency_overrides[require_bearer] = lambda: None
    yield
    app.dependency_overrides.pop(require_bearer, None)


MOCK_ISSUE = GitHubIssue.model_validate(
    {
        "title": "Add foo feature",
        "body": "We need foo.",
        "user": {"login": "ColinCee"},
    }
)

MOCK_CLI_RESULT = CLIResult(
    output="done",
    total_premium_requests=5,
    session_id="sess-123",
    session_time_seconds=120,
    api_time_seconds=60,
    models={"gpt-5.4": "883.6k in, 17.7k out, 788.5k cached"},
    tokens_line="↑ 883.6k • ↓ 17.7k • 788.5k (cached)",
)

IMPLEMENT_ENV = {
    "TASK_TYPE": "implement",
    "REPO": "user/repo",
    "NUMBER": "10",
    "GH_TOKEN": "ghs_test",
}

REVIEW_ENV = {
    "TASK_TYPE": "review",
    "REPO": "user/repo",
    "NUMBER": "42",
    "GH_TOKEN": "ghs_test",
}


@pytest.fixture()
def make_mock_process():
    """Factory fixture returning mock subprocess objects for copilot tests."""

    def _factory(stdout_text: str = "", returncode: int = 0):
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

    return _factory
