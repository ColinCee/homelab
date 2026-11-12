import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


def pytest_sessionstart(session: pytest.Session) -> None:
    # The gateway tests' image downloads while the other tests run: it was the slowest part of CI.
    if hasattr(session.config, "workerinput") or not shutil.which("docker"):
        return
    compose = Path(__file__).parent.parent / "stacks/observability/compose.yaml"
    image = yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]["gateway"]["image"]
    quiet = subprocess.DEVNULL
    subprocess.Popen(
        ["docker", "pull", "-q", image], stdout=quiet, stderr=quiet, start_new_session=True
    )


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
