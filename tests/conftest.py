import os

import pytest


@pytest.fixture(autouse=True)
def _no_outer_git(monkeypatch: pytest.MonkeyPatch) -> None:
    # An inherited GIT_DIR or GIT_WORK_TREE sends a test's `git init` into the caller's repository.
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
