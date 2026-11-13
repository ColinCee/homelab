"""ci-checkout (ci-runner/rootfs/ci_checkout.py): the job's commit in the workspace, with a pull
request's base, from a real git server."""

import importlib.util
from pathlib import Path

import pytest

from conftest import ROOT, git

_spec = importlib.util.spec_from_file_location(
    "ci_checkout", ROOT / "ci-runner/rootfs/ci_checkout.py"
)
assert _spec is not None and _spec.loader is not None
checkout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checkout)


@pytest.fixture
def server(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo as GitHub would serve it: a base commit and a merge commit on top."""
    repo = tmp_path / "server" / "owner" / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    (repo / "a.txt").write_text("base\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("merge\n")
    git(repo, "commit", "-q", "-am", "merge")
    return tmp_path / "server", base, git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize(("event", "depth"), [("pull_request", 2), ("push", 1)])
def test_the_job_commit_is_checked_out_with_a_pull_request_s_base(
    server: tuple[Path, str, str], tmp_path: Path, event: str, depth: int
) -> None:
    root, base, merge = server
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "left over").write_text("from before")
    env = {
        "GITHUB_SHA": merge,
        "GITHUB_SERVER_URL": root.as_uri(),
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_EVENT_NAME": event,
        "GITHUB_WORKSPACE": str(workspace),
    }
    assert checkout.main(env) == 0
    assert git(workspace, "rev-parse", "HEAD") == merge
    assert (workspace / "a.txt").read_text() == "merge\n"
    assert not (workspace / "left over").exists()
    assert git(workspace, "remote", "get-url", "origin") == f"{root.as_uri()}/owner/repo"
    assert len(git(workspace, "log", "--format=%H").split()) == depth
    if depth == 2:
        assert git(workspace, "rev-parse", "HEAD^1") == base


def test_a_failed_fetch_fails_the_step(server: tuple[Path, str, str], tmp_path: Path) -> None:
    root, _, _ = server
    env = {
        "GITHUB_SHA": "0" * 40,
        "GITHUB_SERVER_URL": root.as_uri(),
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_WORKSPACE": str(tmp_path / "work"),
    }
    assert checkout.main(env) != 0
