import json
import os
import shutil
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

# CI's time limits, which fail the run in CI: the whole run at about twice today's 10-15 s, and one
# test's own body (a sleep, a timeout, a container started per test). Shared fixtures count in the
# whole run. Raise one only in a pull request that says why.
RUN_LIMIT = 30.0
TEST_LIMIT = 5.0

# Each test's first start and its seconds over setup, call and teardown, in the controller.
TIMES: dict[str, list[float]] = {}
CALLS: dict[str, float] = {}
STARTED = [time.monotonic()]


def pytest_sessionstart(session: pytest.Session) -> None:
    STARTED[0] = time.monotonic()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    start, took = TIMES.get(report.nodeid, [report.start, 0.0])
    TIMES[report.nodeid] = [min(start, report.start), took + report.stop - report.start]
    if report.when == "call":
        CALLS[report.nodeid] = report.stop - report.start


def over_limits(run_seconds: float, calls: dict[str, float]) -> list[str]:
    lines = [
        f"{test} took {took:.1f} s, over TEST_LIMIT ({TEST_LIMIT:.0f} s)"
        for test, took in calls.items()
        if took > TEST_LIMIT
    ]
    if run_seconds > RUN_LIMIT:
        lines.append(f"The run took {run_seconds:.0f} s, over RUN_LIMIT ({RUN_LIMIT:.0f} s)")
    return lines


def pytest_sessionfinish(session: pytest.Session) -> None:
    if hasattr(session.config, "workerinput"):
        return
    over = over_limits(time.monotonic() - STARTED[0], CALLS)
    for line in over:
        print()
        print(f"{line}. Make it faster, or raise the limit here in a pull request saying why.")
    if over and os.environ.get("CI") and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    # The slowest tests for scripts/ci_times.py, which ci.yaml posts as an annotation.
    if out := os.environ.get("TEST_TIMES"):
        write_slowest(Path(out))


def write_slowest(out: Path) -> None:
    """[[test, start, seconds], ...], slowest first, under the 4096 characters GitHub keeps of an
    annotation (with room for escaping)."""
    rows: list[list] = []
    for test, (start, took) in sorted(TIMES.items(), key=lambda item: -item[1][1]):
        row = [test[-150:], round(start, 2), round(took, 2)]
        if len(json.dumps([*rows, row], separators=(",", ":"))) > 3500:
            break
        rows.append(row)
    out.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")


def tool(name: str) -> str:
    """The path of a mise.toml tool such as promtool: skipped without it, but failed in CI."""
    path = shutil.which(name)
    if path:
        return path
    if os.environ.get("CI"):
        pytest.fail(f"{name} is not on PATH; mise.toml installs it")
    pytest.skip(f"needs {name} (mise install)")


class NoGitHub:
    """scripts/ci_supervisor.py's GitHub for code that must not ask it. A fake subclasses it and
    gives `request` GitHub's answers."""

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> object:
        raise AssertionError(f"GitHub was asked: {method} {path}")

    def forget(self, drop: Callable[[str], bool]) -> None:
        pass

    def clone(self) -> NoGitHub:
        return self


@pytest.fixture(autouse=True)
def _no_outer_git(monkeypatch: pytest.MonkeyPatch) -> None:
    # An inherited GIT_DIR or GIT_WORK_TREE sends a test's `git init` into the caller's repository.
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
