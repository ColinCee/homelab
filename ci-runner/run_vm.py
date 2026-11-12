"""Boot one throwaway microVM in a slot, feed it a task through MMDS, wait for it to finish, and
delete everything it wrote:
    sudo run-vm.sh SLOT METADATA_JSON [TIMEOUT_SECONDS]
The guest console is printed to stdout. The jailer runs Firecracker as the slot's own user,
chrooted, with a syscall filter, under cgroup limits.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from vm_disks import (
    Failed,
    Host,
    Process,
    Run,
    attach_disk,
    check,
    detach_disks,
    make_dir,
    written_bytes,
)

USAGE = "usage: run-vm.sh SLOT METADATA_JSON [TIMEOUT]"
HERE = Path(__file__).parent
# 6 GiB for the tar (CACHE_MAX_UNPACKED_BYTES in ci_supervisor.py) and its header.
OUTBOX_BYTES = 6 * 1024 * 1024 * 1024 + 1024 * 1024
# The fc-cache action tells the outbox from any other disk by this first line.
OUTBOX_MARK = b"homelab-ci-outbox\n"
OUTBOX_DRIVE = (
    ', {"drive_id": "outbox", "path_on_host": "outbox.img",'
    ' "is_root_device": false, "is_read_only": false}'
)
CACHE_DRIVE = (
    ', {"drive_id": "cache", "path_on_host": "cache.ext4",'
    ' "is_root_device": false, "is_read_only": false}'
)
GUEST_ARGS = re.compile(r"[a-z0-9=_,. -]*")


class Signals:
    """A stop signal is held, not raised, so a setup command is never cut short: the run ends
    after the command it came in, and the cleanup always runs. The jailer is the exception
    (`run_jailer`): it can run for the job's whole timeout, so a signal kills the VM at once."""

    def __init__(self) -> None:
        self.pending: int | None = None

    def catch(self, signum: int, frame: FrameType | None) -> None:
        if self.pending is None:
            self.pending = signum

    def install(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, self.catch)

    def checked(self, run: Run) -> Run:
        """`run`, ending the run once a command returns after a signal."""

        def checked_run(
            command: Sequence[str], *, out: bool = False, quiet: bool = False
        ) -> subprocess.CompletedProcess[str]:
            result = run(command, out=out, quiet=quiet)
            if self.pending is not None:
                raise Failed(128 + self.pending)
            return result

        return checked_run


class Sampler:
    """The VM's largest use of its own memory (anon in its cgroup: not the host's file cache),
    sampled every 2 s and printed when it ends, to size VMs by kind of job."""

    def __init__(self, memory_stat: Path) -> None:
        self.memory_stat = memory_stat
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def start(self) -> Sampler:
        self._thread.start()
        return self

    def stop(self) -> int:
        self._stop.set()
        self._thread.join()
        return self.peak

    def _sample(self) -> None:
        while not self._stop.wait(2):
            try:
                lines = self.memory_stat.read_text().splitlines()
            except OSError:
                continue
            for line in lines:
                name, _, value = line.partition(" ")
                if name == "anon" and value.strip().isdigit():
                    self.peak = max(self.peak, int(value))


@dataclass(frozen=True)
class End:
    """How the VM ended, for the line the supervisor's alerts and people read."""

    status: str
    peak_bytes: int
    mem_mib: int
    cache_out: str


@dataclass(frozen=True)
class Slot:
    number: int
    host: Host

    @property
    def id(self) -> str:
        return f"ci-slot{self.number}"

    @property
    def jail(self) -> Path:
        return self.host.path("/srv/jailer")

    @property
    def chroot(self) -> Path:
        return self.jail / "firecracker" / self.id / "root"

    @property
    def cgroup(self) -> Path:
        return self.host.path(f"/sys/fs/cgroup/firecracker/{self.id}")


def end_line(host: Host, slot: int, end: End) -> str:
    wrote = written_bytes(host, slot, [Path(end.cache_out)] if end.cache_out else [])
    return (
        f"slot {slot}: VM ended ({end.status}), its memory peaked at"
        f" {end.peak_bytes // 1048576} MiB of {end.mem_mib},"
        f" it wrote {'?' if wrote is None else wrote // 1048576} MiB"
    )


def cleanup(slot: Slot, end: End | None) -> None:
    """Kill anything left in the slot's cgroup (say, after a run was killed) before deleting its
    files, so nothing of the slot user's survives."""
    host = slot.host
    if slot.cgroup.is_dir():
        with contextlib.suppress(OSError):
            (slot.cgroup / "cgroup.kill").write_text("1\n")
        for _ in range(50):
            try:
                slot.cgroup.rmdir()
                break
            except OSError:
                host.sleep(0.1)
    try:
        if end:
            # What it wrote to the SSD too, to find the VMs that wear it most.
            print(end_line(host, slot.number, end), file=sys.stderr, flush=True)
    finally:
        try:
            detach_disks(host, slot.number)
        finally:
            shutil.rmtree(slot.jail / "firecracker" / slot.id, ignore_errors=True)


def copy(source: Path, target: Path, mode: int) -> None:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with source.open("rb") as src, open(fd, "wb") as dst:
        shutil.copyfileobj(src, dst)
        os.fchmod(dst.fileno(), mode)


def vm_config(slot: int, vcpus: int, mem_mib: int, guest_args: str, drives: str) -> str:
    mac = f"06:00:0a:c8:{slot:02x}:02"
    return f"""{{
  "boot-source": {{
    "kernel_image_path": "vmlinuz",
    "boot_args": "console=ttyS0 reboot=k panic=1 quiet loglevel=3 ci.ip=10.200.{slot}.2/30 ci.gw=10.200.{slot}.1{guest_args}"
  }},
  "drives": [
    {{"drive_id": "rootfs", "path_on_host": "rootfs.ext4", "is_root_device": true, "is_read_only": false}}{drives}
  ],
  "network-interfaces": [
    {{"iface_id": "eth0", "guest_mac": "{mac}", "host_dev_name": "fc-tap{slot}"}}
  ],
  "balloon": {{"amount_mib": 0, "deflate_on_oom": false, "stats_polling_interval_s": 0, "free_page_reporting": true}},
  "machine-config": {{"vcpu_count": {vcpus}, "mem_size_mib": {mem_mib}}},
  "mmds-config": {{"version": "V2", "network_interfaces": ["eth0"]}}
}}
"""  # noqa: E501


def read_guest_args(host: Host) -> str:
    """Extra guest kernel arguments, for experiments: root-only file, plain words only."""
    path = host.path("/etc/homelab-ci/guest-args")
    if not path.is_file():
        return ""
    extra = path.read_bytes().decode(errors="surrogateescape").replace("\n", " ")
    if not GUEST_ARGS.fullmatch(extra):
        raise Failed(2, "bad guest-args")
    return " " + extra.removesuffix(" ")


def make_outbox(slot: Slot, cache_out: Path) -> bool:
    """A raw disk, last in the VM's list, where a green default-branch job leaves its new cache
    (fc-cache action). It is not a snapshot: the file stays after the VM, for the supervisor to
    judge and turn into the next cache disk."""
    cache_out.unlink(missing_ok=True)
    fd = os.open(cache_out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with open(fd, "wb") as f:
        os.fchown(f.fileno(), *slot.host.root_ids)
        os.fchmod(f.fileno(), 0o600)
        f.write(OUTBOX_MARK)
        f.truncate(OUTBOX_BYTES)  # sparse: takes only what is written
    # A link, not a copy: the chroot and the outbox folder are on one filesystem. If not, the VM
    # goes without an outbox and its cache is simply not saved.
    try:
        os.link(cache_out, slot.chroot / "outbox.img")
    except OSError as e:
        print(f"ln: {e}", file=sys.stderr)
        print(
            f"slot {slot.number}: no outbox, {cache_out} cannot be linked into the chroot",
            file=sys.stderr,
            flush=True,
        )
        cache_out.unlink(missing_ok=True)
        return False
    return True


@dataclass(frozen=True)
class Job:
    metadata: Path
    timeout: str
    uid: int
    gid: int
    vcpus: int
    mem_mib: int
    cache_image: str
    cache_out: str


def jailer_command(slot: Slot, job: Job) -> list[str]:
    """Memory capped a little above the guest's, with no swap: the cap also counts the host's
    cache of the VM's own disk, and at the cap the kernel swapped guest RAM out rather than drop
    that cache. Guest RAM plus Firecracker's own (under 100 MiB) fits in the cap, so only the
    cache goes."""
    memory_max = (job.mem_mib + 256) * 1024 * 1024
    return [
        "timeout", "--kill-after=10", job.timeout, "/usr/local/bin/jailer",
        "--id", slot.id, "--exec-file", "/usr/local/bin/firecracker",
        "--uid", str(job.uid), "--gid", str(job.gid), "--chroot-base-dir", str(slot.jail),
        "--cgroup-version", "2", "--cgroup", "cpu.weight=20",
        "--cgroup", f"memory.max={memory_max}", "--cgroup", "memory.swap.max=0",
        "--", "--config-file", "vm.json", "--no-api", "--metadata", "metadata.json",
        "--level", "Warning",
    ]  # fmt: skip


GRACE_SECONDS = 5


def stop_process(slot: Slot, process: Process) -> None:
    """Kill the VM now. Firecracker is not in the unit's cgroup (the jailer moves it into the
    slot's own), so stopping the unit does not reach it: kill that cgroup, and if the jailer is
    still there, its process group."""
    with contextlib.suppress(OSError):
        (slot.cgroup / "cgroup.kill").write_text("1\n")
    for signum in (None, signal.SIGTERM, signal.SIGKILL):
        if signum is not None:
            process.kill_group(signum)
        try:
            process.wait(GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            continue
        return


def run_jailer(slot: Slot, job: Job, signals: Signals) -> int:
    """Run the jailer to its end and return its exit status; a stop signal kills the VM and fails
    the run as it does any other command."""
    process = slot.host.spawn(jailer_command(slot, job))
    while True:
        if signals.pending is not None:
            stop_process(slot, process)
            raise Failed(128 + signals.pending)
        try:
            code = process.wait(0.2)
        except subprocess.TimeoutExpired:
            continue
        # A command killed by a signal, as a shell reports it.
        return code if code >= 0 else 128 - code


def boot(slot: Slot, job: Job, signals: Signals, ended: list[End]) -> int:
    """Fill the chroot, run the VM to its end and return its exit status; how it ended goes in
    `ended` even when it fails."""
    host, chroot, n, uid, gid = slot.host, slot.chroot, slot.number, job.uid, job.gid
    # Fill the chroot as root, then hand it over, so the slot user cannot plant links for root
    # to follow.
    make_dir(host, chroot, 0o700)
    images = host.path("/var/lib/homelab-ci/images")
    # The disks are snapshots of the images (vm_disks.py), not copies: copying the 8 GB image
    # took up to 10 minutes with ten VMs starting at once.
    attach_disk(host, n, "root", images / "rootfs.ext4", chroot / "rootfs.ext4")
    copy(images / "vmlinuz", chroot / "vmlinuz", 0o600)
    copy(job.metadata, chroot / "metadata.json", 0o600)
    # The repo's cache disk, if the supervisor names one: a snapshot, like the root disk, so the
    # job can write to it and nothing it writes reaches the next job.
    cache = bool(job.cache_image) and Path(job.cache_image).is_file()
    if cache:
        attach_disk(host, n, "cache", Path(job.cache_image), chroot / "cache.ext4")
    outbox = bool(job.cache_out) and make_outbox(slot, Path(job.cache_out))
    guest_args = read_guest_args(host)

    drives = (CACHE_DRIVE if cache else "") + (OUTBOX_DRIVE if outbox else "")
    config = vm_config(n, job.vcpus, job.mem_mib, guest_args, drives)
    fd = os.open(chroot / "vm.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with open(fd, "w") as f:
        f.write(config)
    for name in ("rootfs.ext4", "vmlinuz", "vm.json"):
        os.chmod(chroot / name, 0o600)
    for name in ("rootfs.ext4", "vmlinuz", "metadata.json", "vm.json"):
        os.chown(chroot / name, uid, gid, follow_symlinks=False)
    os.chown(chroot, uid, gid, follow_symlinks=False)
    if cache:
        os.chmod(chroot / "cache.ext4", 0o600)
        os.chown(chroot / "cache.ext4", uid, gid, follow_symlinks=False)
    if outbox:
        os.chown(chroot / "outbox.img", uid, gid)

    # Below the services in CPU priority. cpu.weight only ranks a cgroup against its siblings:
    # the slot's own weight ranks the VMs against each other, the parent's ranks them all against
    # system.slice, where the services run at 100.
    slot.cgroup.parent.mkdir(parents=True, exist_ok=True)
    (slot.cgroup.parent / "cpu.weight").write_text("20\n")
    sampler = Sampler(slot.cgroup / "memory.stat").start()
    status = "stopped"
    try:
        code = run_jailer(slot, job, signals)
        status = f"exit {code}"
        return code
    finally:
        ended.append(End(status, sampler.stop(), job.mem_mib, job.cache_out))


def lock_slot(host: Host, slot: int) -> int:
    """One VM per slot at a time. The VM does not inherit the lock: if this run dies, the next
    one kills the orphan."""
    run_dir = host.path("/run/homelab-ci")
    make_dir(host, run_dir, 0o700)
    fd = os.open(run_dir / f"slot{slot}.lock", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise Failed(75, f"slot {slot} is busy") from None
    return fd


def main(argv: Sequence[str], env: Mapping[str, str], host: Host, signals: Signals) -> int:
    try:
        return run_vm(argv, env, host, signals)
    except Failed as e:
        if e.message:
            print(e.message, file=sys.stderr, flush=True)
        return e.status
    except OSError as e:
        print(f"run-vm: {e}", file=sys.stderr, flush=True)
        return 1


def positive(env: Mapping[str, str], name: str, default: int) -> int:
    value = env.get(name) or str(default)
    if not re.fullmatch(r"[0-9]+", value) or int(value) == 0:
        raise Failed(2, f"bad {name}")
    return int(value)


def run_vm(argv: Sequence[str], env: Mapping[str, str], host: Host, signals: Signals) -> int:
    if len(argv) < 2 or not argv[0] or not argv[1]:
        raise Failed(1, USAGE)
    if not re.fullmatch(r"[0-9]+", argv[0]):
        raise Failed(2, "bad slot")
    n = int(argv[0])
    vcpus, mem_mib = positive(env, "VCPUS", 2), positive(env, "MEM_MIB", 2048)
    user = f"fc-slot{n}"
    job = Job(
        metadata=Path(argv[1]),
        timeout=argv[2] if len(argv) > 2 and argv[2] else "3600",
        uid=int(check(host, ["id", "-u", user], out=True)),
        gid=int(check(host, ["id", "-g", user], out=True)),
        vcpus=vcpus,
        mem_mib=mem_mib,
        cache_image=env.get("CACHE_IMAGE", ""),
        cache_out=env.get("CACHE_OUT", ""),
    )
    checked = dataclasses.replace(host, run=signals.checked(host.run))
    slot = Slot(n, checked)

    lock = lock_slot(host, n)
    # Block the home's current public address; refuse to start without it.
    if checked.run([str(HERE / "net" / "setup-net.sh"), "wan"]).returncode != 0:
        raise Failed(1, "public address not blocked")
    # And refuse a slot user the network wall does not cover (homelab-ci.nft, slot_uids).
    wall = ["nft", "get", "element", "inet", "homelab_ci", "slot_uids", f"{{ {job.uid} }}"]
    if checked.run(wall, quiet=True).returncode != 0:
        raise Failed(1, f"{user} is not in the network wall; run setup-net.sh up")

    # Cleaning up never stops for a signal, so it runs on the unchecked host.
    cleanup(Slot(n, host), None)
    ended: list[End] = []
    try:
        return boot(slot, job, signals, ended)
    finally:
        cleanup(Slot(n, host), ended[0] if ended else None)
        # The one-job registration goes with the VM (not at the start: it is this run's).
        job.metadata.unlink(missing_ok=True)
        os.close(lock)


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("run with sudo", file=sys.stderr)
        sys.exit(1)
    caught = Signals()
    caught.install()
    code = main(sys.argv[1:], os.environ, Host(), caught)
    if caught.pending is not None:
        # Ended by the signal, as a shell is, so the supervisor sees how it ended.
        signal.signal(caught.pending, signal.SIG_DFL)
        os.kill(os.getpid(), caught.pending)
    sys.exit(code)
