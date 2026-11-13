from pathlib import Path

import pytest

from checks.versions_hash import VERSIONS, problems
from conftest import ROOT, git

RIGHT = "a" * 64
OLD = f"GH_VERSION=v2.0.0\nGH_SHA256={'0' * 64}\nPYTHON_VERSION=3.15\n"
GH_URL = "https://github.com/cli/cli/releases/download/v2.1.0/gh_2.1.0_linux_amd64.tar.gz"


def _commit(repo: Path, text: str) -> str:
    path = repo / VERSIONS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "c")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    git(tmp_path, "init", "-q")
    monkeypatch.chdir(tmp_path)
    return _commit(tmp_path, OLD)


def _fetch(url: str) -> str:
    assert url == GH_URL
    return RIGHT


def test_new_version_with_wrong_hash_fails_naming_the_right_one(base: str) -> None:
    head = _commit(Path.cwd(), f"GH_VERSION=v2.1.0\nGH_SHA256={'b' * 64}\nPYTHON_VERSION=3.15\n")
    assert problems(base, head, _fetch) == [f"GH v2.1.0: wrong hash; set GH_SHA256={RIGHT}"]


def test_new_version_with_right_hash_passes(base: str) -> None:
    head = _commit(Path.cwd(), f"GH_VERSION=v2.1.0\nGH_SHA256={RIGHT}\nPYTHON_VERSION=3.16\n")
    assert problems(base, head, _fetch) == []


def test_unchanged_tools_are_not_downloaded(base: str) -> None:
    head = _commit(Path.cwd(), OLD + "# a comment\n")

    def no_fetch(url: str) -> str:
        raise AssertionError(url)

    assert problems(base, head, no_fetch) == []


def test_a_failed_download_fails_naming_the_tool(base: str) -> None:
    head = _commit(Path.cwd(), f"GH_VERSION=v2.1.0\nGH_SHA256={RIGHT}\n")

    def offline(url: str) -> str:
        raise OSError("HTTP Error 404")

    assert problems(base, head, offline) == [f"GH: could not download {GH_URL}: HTTP Error 404"]


def test_a_new_tool_without_a_url_fails(base: str) -> None:
    head = _commit(Path.cwd(), OLD + f"NEWTOOL_VERSION=1\nNEWTOOL_SHA256={RIGHT}\n")
    assert problems(base, head, _fetch) == [
        "NEWTOOL: no download URL known; add it to URLS in checks/versions_hash.py"
    ]


def test_every_pinned_tool_here_has_a_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # This checkout's pins, committed in a repository of the test's own, so it runs wherever the
    # checkout's own git does not (a Windows worktree read from WSL).
    pinned = (ROOT / VERSIONS).read_text(encoding="utf-8")
    git(tmp_path, "init", "-q")
    monkeypatch.chdir(tmp_path)
    empty = _commit(tmp_path, "")
    head = _commit(tmp_path, pinned)
    found = problems(empty, head, lambda url: "")
    assert found and all("wrong hash" in line for line in found), found


def test_the_python_build_standalone_asset_is_named_by_python_version_and_tag(base: str) -> None:
    pbs = "PBS_VERSION=20261110\nPBS_SHA256=" + "b" * 64 + "\nPBS_PYTHON=3.15.2\n"
    head = _commit(Path.cwd(), OLD + pbs)
    urls: list[str] = []

    def fetch(url: str) -> str:
        urls.append(url)
        return "b" * 64

    assert problems(base, head, fetch) == []
    assert urls == [
        "https://github.com/astral-sh/python-build-standalone/releases/download/20261110"
        "/cpython-3.15.2%2B20261110-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
    ]
