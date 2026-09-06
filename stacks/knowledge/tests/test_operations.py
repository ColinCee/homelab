import os
import subprocess
from datetime import UTC, datetime, timedelta
from io import BufferedIOBase
from pathlib import Path

import pytest

from stacks.knowledge import operations


def test_ingest_pulls_notes_then_runs_profile_without_shell(tmp_path: Path) -> None:
    notes_dir = tmp_path / "notes with spaces"
    notes_dir.mkdir()
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    operations.ingest_notes(
        notes_dir,
        compose_file=tmp_path / "compose.yaml",
        working_directory=tmp_path,
        environment={"EXISTING": "value"},
        runner=runner,
    )

    assert calls[0][0] == ["git", "pull", "--ff-only"]
    assert calls[0][1]["cwd"] == notes_dir.resolve()
    assert calls[1][0][4:] == [
        "--profile",
        "ingest",
        "run",
        "--rm",
        "ingest",
        "ingest",
        "--dir",
        "/notes",
    ]
    environment = calls[1][1]["env"]
    assert isinstance(environment, dict)
    assert environment["NOTES_DIR"] == str(notes_dir.resolve())
    assert "EXISTING" not in environment
    assert "shell" not in calls[1][1]


def test_backup_validates_before_publishing_and_removes_expired_dumps(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    expired = backup_dir / "knowledge-expired.dump"
    expired.write_bytes(b"old")
    old_time = (datetime.now(UTC) - timedelta(days=30)).timestamp()
    expired.touch()
    os.utime(expired, (old_time, old_time))
    calls: list[list[str]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if "pg_dump" in command[-1]:
            output = kwargs["stdout"]
            assert isinstance(output, BufferedIOBase)
            output.write(b"dump")
        elif command[-1] == "--list":
            dump = kwargs["stdin"]
            assert isinstance(dump, BufferedIOBase)
            assert dump.read() == b"dump"
        return subprocess.CompletedProcess(command, 0)

    result = operations.backup_database(
        operations.BackupSettings(backup_dir, retention_days=14),
        compose_file=tmp_path / "compose.yaml",
        working_directory=tmp_path,
        runner=runner,
        now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )

    assert result.backup_file.read_bytes() == b"dump"
    assert not expired.exists()
    assert not list(backup_dir.glob("*.tmp"))
    assert "--format=custom --no-owner --no-acl" in calls[0][-1]
    assert calls[1][-1] == "--list"


def test_backup_rejects_invalid_retention_before_creating_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        operations.BackupSettings(tmp_path / "backups", retention_days=0)

    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("failed_command", ["dump", "validate"])
def test_failed_backup_preserves_existing_dumps(tmp_path: Path, failed_command: str) -> None:
    existing = tmp_path / "knowledge-existing.dump"
    existing.write_bytes(b"keep")
    old_time = (datetime.now(UTC) - timedelta(days=30)).timestamp()
    os.utime(existing, (old_time, old_time))

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if command[-1] == "--list" or failed_command == "dump":
            raise subprocess.CalledProcessError(1, command)
        output = kwargs["stdout"]
        assert isinstance(output, BufferedIOBase)
        output.write(b"unvalidated")
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(subprocess.CalledProcessError):
        operations.backup_database(
            operations.BackupSettings(tmp_path, retention_days=14), runner=runner
        )

    assert list(tmp_path.iterdir()) == [existing]
    assert existing.read_bytes() == b"keep"
