"""A CI VM's disks without copying their images: each disk is a device-mapper snapshot that reads
the image through a read-only loop device and puts the VM's writes in a sparse file. That file is
deleted as soon as it is open and the loop devices detach themselves once the snapshot is removed,
so whatever the VM wrote is gone with it, even if run_vm.py is killed or the host restarts.
Used by run_vm.py and stop_vms.py, as root.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class Run(Protocol):
    """Runs a command to its end and never raises on its exit status. `out` captures stdout
    (stderr always goes through); `quiet` drops both."""

    def __call__(
        self, command: Sequence[str], *, out: bool = False, quiet: bool = False
    ) -> subprocess.CompletedProcess[str]: ...


def run_command(
    command: Sequence[str], *, out: bool = False, quiet: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if out else subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
        text=True,
        check=False,
    )


class Process(Protocol):
    """A command still running: `wait` raises subprocess.TimeoutExpired when it has not ended."""

    def wait(self, timeout: float) -> int: ...

    def kill_group(self, signum: int) -> None: ...


class Spawned:
    """A command in a process group of its own, so a signal reaches what it started too."""

    def __init__(self, command: Sequence[str]) -> None:
        self.popen = subprocess.Popen(
            list(command), stdin=subprocess.DEVNULL, start_new_session=True
        )

    def wait(self, timeout: float) -> int:
        return self.popen.wait(timeout)

    def kill_group(self, signum: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.popen.pid, signum)


@dataclass(frozen=True)
class Host:
    """The side effects: commands through `run` (to their end) or `spawn` (left running), files
    under `root`."""

    run: Run = run_command
    spawn: Callable[[Sequence[str]], Process] = Spawned
    root: Path = Path("/")
    sleep: Callable[[float], None] = time.sleep
    # Who owns what this makes: root, or a test's own user.
    root_ids: tuple[int, int] = (0, 0)

    def path(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")

    @property
    def cow_dir(self) -> Path:
        return self.path("/var/lib/homelab-ci/cow")


class Failed(Exception):
    """A step failed; the run ends with `status`, as a shell under `set -e` would."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def check(host: Host, command: Sequence[str], *, out: bool = False) -> str:
    result = host.run(command, out=out)
    if result.returncode != 0:
        raise Failed(result.returncode)
    return (result.stdout or "").strip() if out else ""


def make_dir(host: Host, path: Path, mode: int) -> None:
    """`install -d -o root -g root -m MODE`: also resets an existing folder's owner and mode."""
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, *host.root_ids)
    os.chmod(path, mode)


def attach_disk(host: Host, slot: int, name: str, image: Path, node: Path) -> None:
    """Snapshot `image` as /dev/mapper/ci-slotSLOT-NAME and make a device node for it at `node`
    (root's, 0600; the caller hands it over)."""
    dm = f"ci-slot{slot}-{name}"
    if host.run(["dmsetup", "info", dm], quiet=True).returncode == 0:
        raise Failed(1, f"{dm} is still there from an earlier VM and cannot be removed")
    make_dir(host, host.cow_dir, 0o700)
    fd, cow_name = tempfile.mkstemp(prefix=f"slot{slot}-{name}.", dir=host.cow_dir)
    os.close(fd)
    cow = Path(cow_name)
    loops: list[str] = []
    created = marked = False
    try:
        # As big as the image, so it can hold every block the VM writes; sparse, so it only
        # takes what is written. The page cache keeps one copy of the image for all VMs.
        os.truncate(cow, (image.stat().st_size + 511) // 512 * 512)
        base = check(host, ["losetup", "--find", "--show", "--read-only", str(image)], out=True)
        loops.append(base)
        loops.append(check(host, ["losetup", "--find", "--show", str(cow)], out=True))
        cow.unlink(missing_ok=True)
        sectors = check(host, ["blockdev", "--getsz", base], out=True)
        # Non-persistent (N), 4 KiB chunks: the ext4 block size.
        table = f"0 {sectors} snapshot {base} {loops[1]} N 8"
        created = True  # even if create fails midway, removing a missing device is harmless
        check(host, ["dmsetup", "create", dm, "--table", table])
        # Held by the snapshot, so this only marks the loops to detach once it is removed.
        marked = True
        check(host, ["losetup", "-d", *loops])
        major, minor = check(
            host, ["dmsetup", "info", "-c", "--noheadings", "-o", "major,minor", dm], out=True
        ).split(":")
        check(host, ["mknod", "-m", "0600", str(node), "b", major, minor])
    except BaseException:
        # Each command still runs when a signal makes the run fail the one before. Marked loops
        # go with the snapshot: detaching them by name could hit a number another slot has taken.
        undo = [["dmsetup", "remove", "--retry", dm]] if created else []
        if not marked and loops:
            undo.append(["losetup", "-d", *loops])
        for command in undo:
            with contextlib.suppress(Failed):
                host.run(command, quiet=True)
        cow.unlink(missing_ok=True)
        raise


def detach_disks(host: Host, slot: int | None) -> bool:
    """Remove the slot's snapshots, or every CI VM's when `slot` is None; False if one stays.
    Kill the VM first: an open snapshot cannot be removed."""
    prefix = "ci-slot" if slot is None else f"ci-slot{slot}-"
    removed = True
    listed = host.run(["dmsetup", "ls"], out=True).stdout or ""
    for line in listed.splitlines():
        dm = line.split()[0] if line.split() else ""
        if dm.startswith(prefix) and re.match(r"ci-slot[0-9]+-", dm):
            removed = host.run(["dmsetup", "remove", "--retry", dm]).returncode == 0 and removed
    # A write file, or a loop device on one, left when a run died before the snapshot took them
    # over. A deleted file shows as "PATH (deleted)".
    files = f"{host.cow_dir}/slot{'' if slot is None else f'{slot}-'}"
    loops = host.run(["losetup", "-nO", "NAME,BACK-FILE"], out=True).stdout or ""
    left = [
        f[0] for f in map(str.split, loops.splitlines()) if len(f) > 1 and f[1].startswith(files)
    ]
    if left:
        host.run(["losetup", "-d", *left])
    if host.cow_dir.is_dir():
        for cow in host.cow_dir.iterdir():
            if str(cow).startswith(files):
                cow.unlink(missing_ok=True)
    return removed


def written_bytes(host: Host, slot: int, files: Sequence[Path]) -> int | None:
    """How many bytes the slot's VM wrote: the sectors written to each of its snapshots, plus the
    blocks each file that exists takes on disk (the outbox, which is sparse). None when a counter
    cannot be read. Call before detach_disks: the counters go with the snapshots."""
    prefix = f"ci-slot{slot}-"
    listed = host.run(
        ["dmsetup", "info", "-c", "--noheadings", "--separator", " ", "-o", "name,blkdevname"],
        out=True,
        quiet=True,
    )
    total, found = 0, False
    for line in (listed.stdout or "").splitlines():
        dm, _, blk = line.strip().partition(" ")
        if not dm.startswith(prefix):
            continue
        try:
            fields = host.path(f"/sys/block/{blk.strip()}/stat").read_text().split()
        except OSError:
            return None
        if len(fields) < 7 or not fields[6].isascii() or not fields[6].isdigit():
            return None
        total += int(fields[6]) * 512
        found = True
    if not found:
        return None
    for file in files:
        if file.is_file():
            # st_blocks counts 512-byte units on Linux, whatever the filesystem's block size.
            total += file.stat().st_blocks * 512
    return total
