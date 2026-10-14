"""Send each project's data that is not in Git from this PC to Beelink, which backs it up.

A project opts in with a `backup.toml` in its folder (under the projects root):

    # Data of this project that is not in Git (homelab: docs/runbooks/backups.md).
    paths = ["~/.servo/servo.db", "~/.servo/bench"]  # files or folders; ~ is home
    exclude = ["shots", "*.tmp"]                      # names left out below a folder

Each project becomes one uncompressed tar, `<pc>-<project>.tar`, sent with Taildrop
(`tailscale file cp`): no SSH key and no extra sign-in, only the tailnet both machines are on.
Uncompressed so that restic on Beelink stores only what changed from one night to the next. A
SQLite database is copied with SQLite's own backup first, so one in use is copied whole; its
-wal and -shm files are left out.

usage: python scripts/pc_backup.py [--root DIR] [--to DEVICE] [--dry-run]

Standard library only, so any Python 3.11+ on the PC runs it. The last run is written to
~/.pc-backup.json.
"""

import argparse
import fnmatch
import json
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = Path(r"D:\Claude Projects")
DEFAULT_TO = "beelink"
MANIFEST = "backup.toml"
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
SQLITE_SIDE_FILES = ("-wal", "-shm", "-journal")
STATUS = Path.home() / ".pc-backup.json"


@dataclass
class Project:
    name: str
    paths: list[Path]
    exclude: list[str] = field(default_factory=list)


def manifests(root: Path) -> list[Project]:
    """Every project under `root` with a backup.toml, in name order.

    A broken file stops the run: a backup that skips a project unseen is worse than one that fails.
    """
    out = []
    for file in sorted(root.glob(f"*/{MANIFEST}")):
        raw = tomllib.loads(file.read_text(encoding="utf-8"))
        paths = raw.get("paths")
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            raise ValueError(f"{file}: `paths` must be a list of paths")
        exclude = raw.get("exclude", [])
        if not isinstance(exclude, list) or not all(isinstance(p, str) for p in exclude):
            raise ValueError(f"{file}: `exclude` must be a list of names")
        out.append(Project(file.parent.name, [Path(p).expanduser() for p in paths], exclude))
    return out


def _left_out(path: Path, exclude: list[str]) -> bool:
    if path.name.endswith(SQLITE_SIDE_FILES):
        return True
    return any(fnmatch.fnmatch(part, pattern) for part in path.parts for pattern in exclude)


def files_of(project: Project) -> list[Path]:
    """The files to send, folders walked, exclusions applied. A listed path missing is an error."""
    out: list[Path] = []
    for path in project.paths:
        if not path.exists():
            raise FileNotFoundError(f"{project.name}: {path} is listed but missing")
        found = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        out += [p for p in found if not _left_out(p.relative_to(path.parent), project.exclude)]
    return out


def _name_in_tar(path: Path) -> str:
    """Where a file sits in the tar: from the home folder when under it, else from its drive."""
    try:
        return "home/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return path.as_posix().replace(":", "")


def pack(project: Project, into: Path) -> tuple[Path, int]:
    """One tar of the project's files in `into`, and the number of files in it."""
    tar_path = into / f"{socket.gethostname().lower()}-{project.name}.tar"
    files = files_of(project)
    with tarfile.open(tar_path, "w") as tar:
        for path in files:
            if path.suffix.lower() in SQLITE_SUFFIXES:
                copy = into / f"copy-{path.name}"
                with (
                    sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as source,
                    sqlite3.connect(copy) as target,
                ):
                    source.backup(target)
                source.close()
                target.close()
                tar.add(copy, arcname=_name_in_tar(path))
                copy.unlink()
            else:
                tar.add(path, arcname=_name_in_tar(path))
    return tar_path, len(files)


def send(tar_path: Path, to: str) -> None:
    subprocess.run(
        ["tailscale", "file", "cp", str(tar_path), f"{to}:"],
        check=True,
        capture_output=True,
        timeout=1800,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--to", default=DEFAULT_TO, help="the Tailscale device that receives the files"
    )
    parser.add_argument("--dry-run", action="store_true", help="pack, but send nothing")
    args = parser.parse_args(argv)
    report: dict = {"started": time.time(), "to": args.to, "projects": {}}
    failed = False
    with tempfile.TemporaryDirectory(prefix="pc-backup-") as scratch:
        for project in manifests(args.root):
            try:
                tar_path, count = pack(project, Path(scratch))
                size = tar_path.stat().st_size
                if not args.dry_run:
                    send(tar_path, args.to)
                report["projects"][project.name] = {
                    "files": count,
                    "bytes": size,
                    "sent": not args.dry_run,
                }
                print(
                    f"{project.name}: {count} files, {size / 1e6:.1f} MB"
                    + ("" if args.dry_run else f" sent to {args.to}")
                )
            except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
                failed = True
                report["projects"][project.name] = {"error": str(exc)[:300]}
                print(f"{project.name}: FAILED {exc}", file=sys.stderr)
    report["finished"], report["ok"] = time.time(), not failed
    STATUS.write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
