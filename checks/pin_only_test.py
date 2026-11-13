from pathlib import Path

import pytest

from checks.pin_only import is_pin_only
from conftest import commit, git

OLD = "ghcr.io/colincee/app:latest@sha256:" + "a" * 64
NEW = "ghcr.io/colincee/app:latest@sha256:" + "b" * 64
COMPOSE = f"services:\n  app:\n    image: {OLD}\n    restart: unless-stopped\n"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    git(tmp_path, "init", "-q")
    commit(tmp_path, {"stacks/app/compose.yaml": COMPOSE, "stacks/web/compose.yaml": COMPOSE})
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _changed(repo: Path, files: dict[str, str | None]) -> bool:
    base = git(repo, "rev-parse", "HEAD")
    return is_pin_only(base, commit(repo, files))


def test_digest_bumps_in_compose_files_are_quick(repo: Path) -> None:
    bumped = COMPOSE.replace(OLD, NEW)
    assert _changed(repo, {"stacks/app/compose.yaml": bumped, "stacks/web/compose.yaml": bumped})


@pytest.mark.parametrize(
    "files",
    [
        pytest.param(
            {"stacks/app/compose.yaml": COMPOSE.replace(OLD, "app:latest")}, id="unpinned"
        ),
        pytest.param({"stacks/app/compose.yaml": COMPOSE.replace(OLD, NEW + "x")}, id="bad-digest"),
        pytest.param(
            {"stacks/app/compose.yaml": COMPOSE.replace(OLD, NEW).replace("unless-stopped", "no")},
            id="other-line-too",
        ),
        pytest.param(
            {"stacks/app/compose.yaml": COMPOSE + f"  extra:\n    image: {NEW}\n"},
            id="new-service",
        ),
        pytest.param(
            {"stacks/app/compose.yaml": COMPOSE.replace(f"    image: {OLD}\n", "")},
            id="image-removed",
        ),
        pytest.param(
            {"stacks/app/compose.yaml": COMPOSE.replace(OLD, NEW), "scripts/x.py": "x = 1\n"},
            id="script-too",
        ),
        pytest.param({"stacks/new/compose.yaml": COMPOSE.replace(OLD, NEW)}, id="new-stack"),
    ],
)
def test_any_other_change_gets_full_checks(repo: Path, files: dict[str, str | None]) -> None:
    assert not _changed(repo, files)


def test_an_empty_diff_gets_full_checks(repo: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    assert not is_pin_only(head, head)
