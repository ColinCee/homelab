import json
import os
import shutil
from pathlib import Path

import pytest

# Each test's first start and its seconds over setup, call and teardown, in the controller.
TIMES: dict[str, list[float]] = {}


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    start, took = TIMES.get(report.nodeid, [report.start, 0.0])
    TIMES[report.nodeid] = [min(start, report.start), took + report.stop - report.start]


def pytest_sessionfinish(session: pytest.Session) -> None:
    # The slowest tests for scripts/ci_times.py, which ci.yaml posts as an annotation.
    out = os.environ.get("TEST_TIMES")
    if not out or hasattr(session.config, "workerinput"):
        return
    slowest = sorted(TIMES.items(), key=lambda item: -item[1][1])[:50]
    rows = [{"test": t, "start": round(s, 3), "seconds": round(d, 3)} for t, (s, d) in slowest]
    Path(out).write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")


def tool(name: str) -> str:
    """The path of a mise.toml tool such as promtool: skipped without it, but failed in CI."""
    path = shutil.which(name)
    if path:
        return path
    if os.environ.get("CI"):
        pytest.fail(f"{name} is not on PATH; mise.toml installs it")
    pytest.skip(f"needs {name} (mise install)")


@pytest.fixture(autouse=True)
def _no_outer_git(monkeypatch: pytest.MonkeyPatch) -> None:
    # An inherited GIT_DIR or GIT_WORK_TREE sends a test's `git init` into the caller's repository.
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
