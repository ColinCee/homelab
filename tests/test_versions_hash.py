import subprocess
from pathlib import Path

import pytest

from scripts.versions_hash import VERSIONS, problems

RIGHT = "a" * 64
OLD = f"GH_VERSION=v2.0.0\nGH_SHA256={'0' * 64}\nPYTHON_VERSION=3.15\n"
GH_URL = "https://github.com/cli/cli/releases/download/v2.1.0/gh_2.1.0_linux_amd64.tar.gz"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, text: str) -> str:
    path = repo / VERSIONS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    _git(tmp_path, "init", "-q")
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
        "NEWTOOL: no download URL known; add it to URLS in scripts/versions_hash.py"
    ]


def test_every_pinned_tool_here_has_a_url(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = Path(__file__).parent.parent
    monkeypatch.chdir(repo)
    head = _git(repo, "rev-parse", "HEAD")
    empty = _git(repo, "hash-object", "-t", "tree", "/dev/null")
    found = problems(empty, head, lambda url: "")
    assert found and all("wrong hash" in line for line in found), found
