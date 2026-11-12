"""The shared action that saves a repo's cache disk (.github/actions/fc-cache)."""

import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from scripts import ci_supervisor as sup

ACTION = Path(__file__).parent.parent / ".github" / "actions" / "fc-cache"


def test_the_pack_checks_the_supervisors_size_limit() -> None:
    script = (ACTION / "fc_cache.py").read_text()
    assert f"MAX_UNPACKED = {sup.CACHE_MAX_UNPACKED_BYTES}\n" in script


def test_the_outbox_holds_the_largest_tar_the_supervisor_takes() -> None:
    script = (ACTION.parent.parent.parent / "ci-runner" / "run-vm.sh").read_text()
    size = re.search(r"OUTBOX_BYTES=\$\(\((.+)\)\)", script)

    assert size is not None
    assert eval(size.group(1)) >= sup.CACHE_MAX_UNPACKED_BYTES + 512


def save(tar: Path, *disks: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "SUDO": "", "GITHUB_RUN_ID": "70", "GITHUB_RUN_ATTEMPT": "2"}
    return subprocess.run(
        [sys.executable, str(ACTION / "fc_cache.py"), "save", str(tar), *map(str, disks)],
        capture_output=True,
        text=True,
        env=env,
    )


def test_saves_to_the_outbox_disk_what_the_supervisor_takes(tmp_path: Path) -> None:
    (tmp_path / "cache" / "uv").mkdir(parents=True)
    (tmp_path / "cache" / "uv" / "wheel").write_text("w")
    (tmp_path / "work").mkdir()
    assert pack(tmp_path / "cache", tmp_path / "work").returncode == 0
    other, outbox = tmp_path / "vdb", tmp_path / "vdc"
    other.write_bytes(b"\0" * 4096)  # the cache disk: an ext4 starts with zeros
    outbox.write_bytes(b"homelab-ci-outbox\n".ljust(1 << 20, b"\0"))  # as run-vm.sh makes it

    result = save(tmp_path / "cache.tar", other, outbox)

    assert result.returncode == 0, result.stderr
    assert other.read_bytes() == b"\0" * 4096
    with outbox.open("rb") as f:
        header = sup.OUTBOX_HEADER.match(f.read(512))
        assert header is not None and (header[1], header[2]) == (b"70", b"2")
        with tarfile.open(fileobj=f, mode="r|") as tar:
            assert [m.name for m in tar] == ["uv", "uv/wheel"]


def test_fails_without_an_outbox_disk(tmp_path: Path) -> None:
    (tmp_path / "cache.tar").write_bytes(b"")
    (tmp_path / "vdb").write_bytes(b"\0" * 4096)

    result = save(tmp_path / "cache.tar", tmp_path / "vdb", tmp_path / "vdz")

    assert result.returncode != 0
    assert "no outbox disk" in result.stderr


def pack(cache: Path, workspace: Path, *paths: str) -> subprocess.CompletedProcess[str]:
    out = workspace.parent / "cache.tar"
    return subprocess.run(
        [sys.executable, str(ACTION / "fc_cache.py"), "pack", str(cache), str(out), *paths],
        cwd=workspace,
        capture_output=True,
        text=True,
    )


def test_packs_the_cache_and_workspace_paths_so_the_supervisor_unpacks_them(tmp_path: Path) -> None:
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


def test_packs_a_workspace_path_once_when_the_cache_disk_holds_last_times(tmp_path: Path) -> None:
    # The cache disk unpacks web/node_modules to /cache/web; packing /cache/web as well
    # put it in every later cache twice.
    cache, workspace = tmp_path / "cache", tmp_path / "work"
    (cache / "web" / "node_modules").mkdir(parents=True)
    (cache / "web" / "node_modules" / "old.js").write_text("old")
    (cache / "uv").mkdir()
    (workspace / "web" / "node_modules").mkdir(parents=True)
    (workspace / "web" / "node_modules" / "new.js").write_text("new")

    result = pack(cache, workspace, "web/node_modules")
    assert result.returncode == 0, result.stderr

    with tarfile.open(tmp_path / "cache.tar") as tar:
        names = tar.getnames()
    assert sorted(names) == ["uv", "web/node_modules", "web/node_modules/new.js"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_leaves_out_what_it_cannot_read(tmp_path: Path) -> None:
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


def test_packs_an_empty_cache(tmp_path: Path) -> None:
    (tmp_path / "cache").mkdir()
    (tmp_path / "work").mkdir()

    assert pack(tmp_path / "cache", tmp_path / "work").returncode == 0
    with tarfile.open(tmp_path / "cache.tar") as tar:
        assert tar.getnames() == []


def test_fails_when_a_named_workspace_path_is_missing(tmp_path: Path) -> None:
    (tmp_path / "cache").mkdir()
    (tmp_path / "work").mkdir()

    result = pack(tmp_path / "cache", tmp_path / "work", "web/node_modules")

    assert result.returncode != 0
    assert "web/node_modules does not exist" in result.stderr


def test_a_missing_cache_folder_is_an_empty_one_and_the_workspace_paths_still_pack(
    tmp_path: Path,
) -> None:
    (tmp_path / "work" / "web").mkdir(parents=True)
    (tmp_path / "work" / "web" / "lock.json").write_text("{}")
    assert pack(tmp_path / "none", tmp_path / "work", "web/lock.json").returncode == 0
    with tarfile.open(tmp_path / "cache.tar") as tar:
        assert tar.getnames() == ["web/lock.json"]


def test_a_hidden_cache_folder_is_left_out(tmp_path: Path) -> None:
    (tmp_path / "cache" / ".hidden").mkdir(parents=True)
    (tmp_path / "cache" / "uv").mkdir()
    (tmp_path / "work").mkdir()
    assert pack(tmp_path / "cache", tmp_path / "work").returncode == 0
    with tarfile.open(tmp_path / "cache.tar") as tar:
        assert tar.getnames() == ["uv"]


def test_the_action_never_fails_the_job_and_leaves_no_tar(tmp_path: Path) -> None:
    # The pack works, the save finds no outbox disk: a warning, never a failed check.
    (tmp_path / "cache" / "uv").mkdir(parents=True)
    env = {
        **os.environ,
        "RUNNER_TEMP": str(tmp_path),
        "FC_CACHE_DIR": str(tmp_path / "cache"),
        "FC_CACHE_DISKS": str(tmp_path / "no-disk"),
        "SUDO": "",
    }
    result = subprocess.run(
        [sys.executable, str(ACTION / "fc_cache.py")],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    assert result.returncode == 0
    assert "no outbox disk" in result.stderr and "was not saved" in result.stderr
    assert not (tmp_path / "cache.tar").exists()
