#!/usr/bin/env python3
"""Host-side operational commands owned by the knowledge stack."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

CommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]
REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = Path(__file__).resolve().parent / "compose.yaml"
DEFAULT_NOTES_DIR = Path("/home/colin/code/notes")
DEFAULT_BACKUP_DIR = Path("/home/colin/backups/knowledge")
DEFAULT_RETENTION_DAYS = 14


@dataclass(frozen=True)
class BackupSettings:
    directory: Path
    retention_days: int

    def __post_init__(self) -> None:
        if self.retention_days < 1:
            raise ValueError("KNOWLEDGE_BACKUP_RETENTION_DAYS must be a positive integer")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> BackupSettings:
        values = os.environ if environment is None else environment
        raw_retention = values.get("KNOWLEDGE_BACKUP_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
        try:
            retention_days = int(raw_retention)
        except ValueError as exc:
            raise ValueError("KNOWLEDGE_BACKUP_RETENTION_DAYS must be a positive integer") from exc
        return cls(
            directory=Path(
                values.get("KNOWLEDGE_BACKUP_DIR") or str(DEFAULT_BACKUP_DIR)
            ).expanduser(),
            retention_days=retention_days,
        )


@dataclass(frozen=True)
class BackupResult:
    backup_file: Path
    size_bytes: int
    retained_count: int
    retention_days: int

    def event(self) -> dict[str, str | int]:
        return {
            "event": "knowledge_backup_completed",
            "status": "success",
            "backup_file": str(self.backup_file),
            "size_bytes": self.size_bytes,
            "retained_count": self.retained_count,
            "retention_days": self.retention_days,
        }


def ingest_notes(
    notes_dir: Path,
    *,
    compose_file: Path = COMPOSE_FILE,
    working_directory: Path = REPO_ROOT,
    environment: Mapping[str, str] | None = None,
    runner: CommandRunner = subprocess.run,
) -> None:
    """Fast-forward the notes checkout and run the containerized ingest job."""
    notes_dir = notes_dir.expanduser().resolve()
    if not notes_dir.is_dir():
        raise ValueError(f"notes directory not found: {notes_dir}")

    runner(["git", "pull", "--ff-only"], cwd=notes_dir, check=True)
    compose_environment = _compose_environment(os.environ if environment is None else environment)
    compose_environment["NOTES_DIR"] = str(notes_dir)
    runner(
        [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "--profile",
            "ingest",
            "run",
            "--rm",
            "ingest",
            "ingest",
            "--dir",
            "/notes",
        ],
        cwd=working_directory,
        env=compose_environment,
        check=True,
    )


def backup_database(
    settings: BackupSettings,
    *,
    compose_file: Path = COMPOSE_FILE,
    working_directory: Path = REPO_ROOT,
    environment: Mapping[str, str] | None = None,
    runner: CommandRunner = subprocess.run,
    now: datetime | None = None,
) -> BackupResult:
    """Create, validate, publish, and retain an atomic Postgres dump."""
    settings.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.directory.chmod(0o700)
    compose_environment = _compose_environment(os.environ if environment is None else environment)
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    backup_file = settings.directory / f"knowledge-{timestamp:%Y%m%dT%H%M%SZ}.dump"
    temporary_file = settings.directory / f".{backup_file.name}.{uuid.uuid4().hex}.tmp"
    postgres = ["docker", "compose", "-f", str(compose_file), "exec", "-T", "postgres"]

    try:
        with temporary_file.open("xb") as output:
            temporary_file.chmod(0o600)
            runner(
                [
                    *postgres,
                    "sh",
                    "-c",
                    'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
                    "--format=custom --no-owner --no-acl",
                ],
                cwd=working_directory,
                env=compose_environment,
                stdout=output,
                check=True,
            )

        with temporary_file.open("rb") as dump:
            runner(
                [*postgres, "pg_restore", "--list"],
                cwd=working_directory,
                env=compose_environment,
                stdin=dump,
                stdout=subprocess.DEVNULL,
                check=True,
            )
        os.replace(temporary_file, backup_file)
    finally:
        temporary_file.unlink(missing_ok=True)

    _remove_expired_backups(settings.directory, settings.retention_days)
    retained = tuple(path for path in settings.directory.glob("knowledge-*.dump") if path.is_file())
    return BackupResult(
        backup_file=backup_file,
        size_bytes=backup_file.stat().st_size,
        retained_count=len(retained),
        retention_days=settings.retention_days,
    )


def _remove_expired_backups(directory: Path, retention_days: int) -> None:
    cutoff = time.time() - retention_days * 24 * 60 * 60
    for path in directory.glob("knowledge-*.dump"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()


def _compose_environment(environment: Mapping[str, str]) -> dict[str, str]:
    allowed = {
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_CERT_PATH",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "NO_COLOR",
        "PATH",
        "TERM",
        "USER",
        "XDG_CONFIG_HOME",
        "XDG_RUNTIME_DIR",
    }
    return {key: value for key, value in environment.items() if key in allowed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("backup", help="dump and validate the Postgres database")
    ingest = commands.add_parser("ingest", help="pull notes and ingest them")
    ingest.add_argument("--notes-dir", type=Path, default=DEFAULT_NOTES_DIR)
    args = parser.parse_args()
    try:
        if args.command == "backup":
            result = backup_database(BackupSettings.from_environment())
            print(json.dumps(result.event(), sort_keys=True))
        else:
            ingest_notes(args.notes_dir)
    except subprocess.CalledProcessError as exc:
        print(f"Error: command exited with {exc.returncode}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
