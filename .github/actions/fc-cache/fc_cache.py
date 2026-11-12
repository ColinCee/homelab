#!/usr/bin/env python3
"""Save a CI VM's cache disk (action.yml): pack /cache and the job's named workspace paths into a
tar, and write it to the VM's outbox disk, which the CI supervisor turns into the repo's next
cache disk once GitHub shows the job passed (scripts/ci_supervisor.py, take_outbox).

    fc_cache.py                      the action: both, from PATHS; never fails the job
    fc_cache.py pack CACHE OUT [PATH...]
    fc_cache.py save TAR [DISK...]

Runs in the VMs with their system Python, for every repo the CI serves: standard library only.
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

# scripts/ci_supervisor.py, CACHE_MAX_UNPACKED_BYTES.
MAX_UNPACKED = 6442450944
# ci-runner/run_vm.py writes it as the outbox disk's first line.
OUTBOX_MARK = b"homelab-ci-outbox\n"


def warn(message: str) -> None:
    print(f"::warning::fc-cache: {message}", file=sys.stderr)


def pack(cache_dir: Path, out: Path, paths: Sequence[str], cwd: Path) -> int:
    """Tar every readable top-level folder of cache_dir (but lost+found) and the workspace paths;
    fail rather than leave an archive the supervisor would refuse."""
    # The cache disk holds the last cache's workspace paths at the same place (web/ for
    # web/node_modules): pack the job's fresh ones, not both, or every cache would carry all
    # earlier copies too.
    tops = {p.removeprefix("./").split("/")[0] for p in paths}
    try:
        entries = list(cache_dir.iterdir())
    except OSError:  # no cache folder: an empty one, and the workspace paths still save
        entries = []
    folders = sorted(
        entry.name
        for entry in entries
        if entry.is_dir()
        and not entry.name.startswith(".")
        and entry.name != "lost+found"
        and entry.name not in tops
        and os.access(entry, os.R_OK | os.X_OK)
    )
    missing = [p for p in paths if not (cwd / p).exists()]
    if missing:
        warn(f"{missing[0]} does not exist")
        return 1
    members = (["-C", str(cache_dir), *folders] if folders else []) + (
        ["-C", str(cwd), *paths] if paths else []
    )
    # Files that cannot be read are left out (tar warns) rather than failing the job;
    # -T /dev/null lets an empty cache make an empty archive.
    command = ["tar", "-cf", str(out), "--ignore-failed-read", "-T", "/dev/null", *members]
    if subprocess.run(command, cwd=cwd).returncode != 0:
        return 1
    size = out.stat().st_size
    if size > MAX_UNPACKED:
        warn(f"{out} is {size} bytes, over the supervisor's {MAX_UNPACKED}")
        return 1
    print(f"fc-cache: {' '.join(folders + list(paths))} -> {size} bytes")
    return 0


def sudo() -> list[str]:
    """Reading and writing a VM's disks takes root; tests set SUDO empty."""
    return os.environ.get("SUDO", "sudo").split()


def outbox_disk(disks: Sequence[str]) -> str | None:
    for disk in disks:
        if not os.path.exists(disk):
            continue
        head = subprocess.run(
            [*sudo(), "head", "-c", str(len(OUTBOX_MARK)), disk], capture_output=True
        )
        if head.stdout == OUTBOX_MARK:
            return disk
    return None


def save(tar: Path, disks: Sequence[str]) -> int:
    """Write the tar after the outbox disk's first 512 bytes, and its header last, so a write cut
    short or refused (the tar too big for the disk) leaves no header and nothing is taken."""
    outbox = outbox_disk(disks or sorted(glob.glob("/dev/vd?")))
    if outbox is None:
        warn("no outbox disk in this VM")
        return 1
    dd = [*sudo(), "dd", f"of={outbox}", "conv=notrunc,fsync", "status=none"]
    body = [*dd, f"if={tar}", "bs=1M", "seek=512", "oflag=seek_bytes"]
    if subprocess.run(body).returncode != 0:
        return 1
    header = f"homelab-ci-cache v1 run={os.environ['GITHUB_RUN_ID']} "
    header += f"attempt={os.environ['GITHUB_RUN_ATTEMPT']}\n"
    if subprocess.run(dd, input=header.encode()).returncode != 0:
        return 1
    print(f"fc-cache: saved to {outbox}")
    return 0


def save_cache() -> int:
    """The action: a cache that is not saved only leaves the next jobs the last one, so it never
    fails the job."""
    tar = Path(os.environ["RUNNER_TEMP"]) / "cache.tar"
    paths = os.environ.get("PATHS", "").split()
    # Tests point these elsewhere: inside a CI VM the defaults are the real disks.
    cache = Path(os.environ.get("FC_CACHE_DIR", "/cache"))
    disks = os.environ.get("FC_CACHE_DISKS", "").split()
    try:
        saved = pack(cache, tar, paths, Path.cwd()) == 0 and save(tar, disks) == 0
    except Exception as e:  # anything at all: the job's own result stands
        warn(f"{type(e).__name__}: {e}")
        saved = False
    finally:
        tar.unlink(missing_ok=True)
    if not saved:
        warn("the cache disk was not saved; the next jobs start from the last one")
    return 0


def main(argv: Sequence[str]) -> int:
    if not argv:
        return save_cache()
    if argv[0] == "pack" and len(argv) >= 3:
        return pack(Path(argv[1]), Path(argv[2]), argv[3:], Path.cwd())
    if argv[0] == "save" and len(argv) >= 2:
        return save(Path(argv[1]), argv[2:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
