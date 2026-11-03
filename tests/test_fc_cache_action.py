"""The shared action that uploads a repo's cache disk (.github/actions/fc-cache)."""

import os
import re
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

from scripts import ci_supervisor as sup

ACTION = Path(__file__).parent.parent / ".github" / "actions" / "fc-cache"


def test_the_action_uploads_what_the_supervisor_reads() -> None:
    action = yaml.safe_load((ACTION / "action.yml").read_text())
    steps = action["runs"]["steps"]
    upload = steps[-1]
    pinned = re.search(
        r"actions/upload-artifact@\w+",
        (ACTION.parent.parent.parent / "ci-runner" / "versions.env").read_text(),
    )

    assert action["runs"]["using"] == "composite"
    assert pinned is not None and upload["uses"] == pinned.group()
    assert upload["with"]["name"] == sup.CACHE_ARTIFACT
    assert upload["with"]["path"] == "${{ runner.temp }}/cache.tar"
    assert upload["with"]["retention-days"] == 3
    assert upload["with"]["compression-level"] == 1
    for step in steps:  # nothing runs off the default branch
        assert step["if"] == (
            "github.event_name == 'push' && "
            "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
        )


def test_the_pack_script_checks_the_supervisors_size_limits() -> None:
    script = (ACTION / "pack.sh").read_text()

    assert f"max_unpacked={sup.CACHE_MAX_UNPACKED_BYTES}\n" in script
    assert f"max_zip={sup.CACHE_MAX_BYTES}\n" in script


def pack(cache: Path, workspace: Path, *paths: str) -> subprocess.CompletedProcess[str]:
    out = workspace.parent / "cache.tar"
    return subprocess.run(
        [str(ACTION / "pack.sh"), str(cache), str(out), *paths],
        cwd=workspace,
        capture_output=True,
        text=True,
    )


def test_packs_the_cache_and_workspace_paths_so_the_supervisor_unpacks_them(tmp_path) -> None:
    cache, workspace = tmp_path / "cache", tmp_path / "work"
    (cache / "uv").mkdir(parents=True)
    (cache / "uv" / "wheel").write_text("w")
    (cache / "venv" / "bin").mkdir(parents=True)
    (cache / "venv" / "bin" / "python").symlink_to("/usr/bin/python3")
    os.link(cache / "uv" / "wheel", cache / "venv" / "wheel")
    (cache / "lost+found").mkdir()
    (cache / "lost+found" / "junk").write_text("x")
    (workspace / "web" / "node_modules" / "a").mkdir(parents=True)
    (workspace / "web" / "node_modules" / "a" / "index.js").write_text("js")
    (workspace / "web" / "package-lock.json").write_text("{}")

    result = pack(cache, workspace, "web/node_modules", "web/package-lock.json")
    assert result.returncode == 0, result.stderr

    root = tmp_path / "unpacked"
    root.mkdir()
    with tarfile.open(tmp_path / "cache.tar") as tar:
        tar.extractall(root, filter=sup.size_capped(sup.CACHE_MAX_UNPACKED_BYTES))
    assert (root / "uv" / "wheel").read_text() == "w"
    assert (root / "venv" / "wheel").read_text() == "w"
    assert os.readlink(root / "venv" / "bin" / "python") == "/usr/bin/python3"
    assert (root / "web" / "node_modules" / "a" / "index.js").read_text() == "js"
    assert (root / "web" / "package-lock.json").read_text() == "{}"
    assert not (root / "lost+found").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_leaves_out_what_it_cannot_read(tmp_path) -> None:
    cache, workspace = tmp_path / "cache", tmp_path / "work"
    (cache / "npm").mkdir(parents=True)
    (cache / "npm" / "secret").write_text("x")
    (cache / "npm" / "secret").chmod(0)
    (cache / "root-only").mkdir()
    (cache / "root-only").chmod(0)
    workspace.mkdir()
    try:
        result = pack(cache, workspace)
    finally:
        (cache / "root-only").chmod(0o755)

    assert result.returncode == 0, result.stderr
    with tarfile.open(tmp_path / "cache.tar") as tar:
        assert tar.getnames() == ["npm"]


def test_packs_an_empty_cache(tmp_path) -> None:
    (tmp_path / "cache").mkdir()
    (tmp_path / "work").mkdir()

    assert pack(tmp_path / "cache", tmp_path / "work").returncode == 0
    with tarfile.open(tmp_path / "cache.tar") as tar:
        assert tar.getnames() == []


def test_fails_when_a_named_workspace_path_is_missing(tmp_path) -> None:
    (tmp_path / "cache").mkdir()
    (tmp_path / "work").mkdir()

    result = pack(tmp_path / "cache", tmp_path / "work", "web/node_modules")

    assert result.returncode != 0
    assert "web/node_modules does not exist" in result.stderr
