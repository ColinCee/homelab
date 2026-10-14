import sqlite3
import tarfile
from pathlib import Path

import pytest

from scripts import pc_backup


def _project(root: Path, name: str, manifest: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "backup.toml").write_text(manifest, encoding="utf-8")
    return folder


def test_each_project_is_one_tar_with_a_database_copied_whole_and_exclusions_left_out(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    data = home / ".thing"
    (data / "shots").mkdir(parents=True)
    (data / "shots" / "big.png").write_bytes(b"png")
    (data / "notes.md").write_text("kept", encoding="utf-8")
    with sqlite3.connect(data / "store.db") as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE t (x)")
        db.execute("INSERT INTO t VALUES (42)")
    monkeypatch.setattr(Path, "home", lambda: home)
    root = tmp_path / "projects"
    _project(root, "thing", f'paths = ["{data.as_posix()}"]\nexclude = ["shots"]\n')
    (root / "no-backup-here").mkdir()  # a project without a manifest is not sent

    [project] = pc_backup.manifests(root)
    tar_path, count = pc_backup.pack(project, tmp_path)
    with tarfile.open(tar_path) as tar:
        names = sorted(tar.getnames())
        assert names == [
            "home/.thing/notes.md",
            "home/.thing/store.db",
        ]  # no picture, no -wal or -shm
        tar.extract("home/.thing/store.db", tmp_path / "out")
    with sqlite3.connect(tmp_path / "out" / "home" / ".thing" / "store.db") as copy:
        assert copy.execute("SELECT x FROM t").fetchall() == [
            (42,)
        ]  # whole, though the original was open in WAL
    assert count == 2 and tar_path.name.endswith("-thing.tar")


def test_a_listed_path_that_is_missing_or_a_broken_manifest_is_an_error_not_a_silent_skip(tmp_path):
    root = tmp_path / "projects"
    _project(root, "gone", f'paths = ["{(tmp_path / "nowhere").as_posix()}"]\n')
    [project] = pc_backup.manifests(root)
    with pytest.raises(FileNotFoundError, match="listed but missing"):
        pc_backup.files_of(project)
    _project(root, "broken", "paths = 'one path, not a list'\n")
    with pytest.raises(ValueError, match="must be a list"):
        pc_backup.manifests(root)


def test_a_run_reports_each_project_and_fails_when_one_fails(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(pc_backup, "STATUS", tmp_path / "status.json")
    monkeypatch.setattr(pc_backup, "send", lambda tar, to: sent.append((tar.name, to)))
    root = tmp_path / "projects"
    good = tmp_path / "good.txt"
    good.write_text("x", encoding="utf-8")
    _project(root, "a", f'paths = ["{good.as_posix()}"]\n')
    assert pc_backup.main(["--root", str(root), "--to", "server"]) == 0
    assert [to for _, to in sent] == ["server"]
    _project(root, "b", f'paths = ["{(tmp_path / "missing").as_posix()}"]\n')
    assert pc_backup.main(["--root", str(root)]) == 1  # one project failing fails the run
