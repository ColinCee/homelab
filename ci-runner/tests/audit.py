#!/usr/bin/env python3
"""Blast-radius audit for the CI runner. Must pass before the supervisor runs, every
quarter, and after any Firecracker, kernel or runner upgrade:
  sudo ci-runner/tests/audit.sh
Checks what a job could reach if it broke out of its VM, or out of Firecracker into the
slot user. Read-only apart from one throwaway VM.
"""

from __future__ import annotations

import fnmatch
import grp
import io
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from host_commands import run_command, start_process

LIB = "/usr/local/lib/homelab-ci"
RUN_VM = f"{LIB}/run-vm.sh"
IMAGE = "/var/lib/homelab-ci/images/rootfs.ext4"
CACHES = "/var/lib/homelab-ci/caches"
SLOT_USER = re.compile(r"fc-slot[0-9]+")
# A real key is a PEM line of its own; npm's docs only quote one inside strings.
# /opt/actions-cache holds public repos at pinned commits (rootfs/Dockerfile), whose
# token redaction code and tests carry example tokens and keys.
SECRETS = r"github_pat_|ghp_[A-Za-z0-9]{30}|^-----BEGIN ([A-Z]+ )?PRIVATE KEY-----[[:space:]]*$"
# Expands inside the VM, not here.
MMDS_PROBE = """curl -sf -X PUT -H "X-metadata-token-ttl-seconds: 60" http://169.254.169.254/latest/api/token > /run/t
echo "MMDS_KEYS $(curl -sf -H "X-metadata-token: $(cat /run/t)" -H "Accept: application/json" http://169.254.169.254/ | jq -c "keys")"
sleep 25"""  # noqa: E501
JAIL_FILES = "dev firecracker firecracker.pid metadata.json rootfs.ext4 run vm.json vmlinuz"
# systemd-resolved's loopback listener; root reaching it shows the probe can succeed.
CONNECT = "</dev/tcp/127.0.0.53/53"
# Matched as words, as grep -w does: a group named docker-users counts too.
PRIVILEGED = re.compile(r"(?<!\w)(docker|sudo|kvm|adm|lxd)(?!\w)")


class Run(Protocol):
    def __call__(
        self, command: list[str], *, env: Mapping[str, str] | None = None, quiet: bool = False
    ) -> subprocess.CompletedProcess[str]: ...


class Waitable(Protocol):
    def wait(self) -> int: ...


def owner_of(path: Path) -> tuple[str, str]:
    """The user and group that own PATH, as GNU stat names them: a link is judged as
    itself, and an id with no name is UNKNOWN."""
    info = path.lstat()
    try:
        user = pwd.getpwuid(info.st_uid).pw_name
    except KeyError:
        user = "UNKNOWN"
    try:
        group = grp.getgrgid(info.st_gid).gr_name
    except KeyError:
        group = "UNKNOWN"
    return user, group


@dataclass
class Host:
    run: Run = run_command
    start: Callable[[list[str], Path], Waitable] = start_process
    root: Path = Path("/")
    sleep: Callable[[float], None] = time.sleep
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    is_root: Callable[[], bool] = lambda: os.geteuid() == 0
    owner: Callable[[Path], tuple[str, str]] = owner_of

    def path(self, name: str) -> Path:
        return self.root / name.lstrip("/")

    # A missing program raises, so the check behind it fails (Audit.check); it is never read as
    # "no" to a question whose "no" passes.
    def ok(self, *command: str, quiet: bool = False) -> bool:
        return self.run(list(command), quiet=quiet).returncode == 0

    def out(self, *command: str, quiet: bool = False) -> str:
        return self.run(list(command), quiet=quiet).stdout


class Audit:
    def __init__(self, host: Host) -> None:
        self.host = host
        self.failures = 0

    def check(self, name: str, test: Callable[[], bool]) -> None:
        """A check that cannot look (a missing file, an unknown user) fails, and the audit
        carries on to the next."""
        try:
            passed = test()
        except OSError, KeyError, ValueError, IndexError, subprocess.SubprocessError:
            passed = False
        if passed:
            print(f"PASS {name}")
        else:
            self.fail(name)

    def fail(self, name: str) -> None:
        print(f"FAIL {name}")
        self.failures += 1

    def owner(self, path: str) -> str:
        return ":".join(self.host.owner(self.host.path(path)))

    # GNU stat and find do not follow links, so a link is judged as itself.
    def mode_is(self, path: str, want: str) -> bool:
        mode = stat.S_IMODE(self.host.path(path).lstat().st_mode)
        return f"{self.owner(path)} {mode:o}" == want

    def root_only_writes(self, path: str) -> bool:
        """Root-owned and writable by root alone; the folder too, as a writable folder or
        file in it fails."""
        return self.owner(path) == "root:root" and not any(
            item.lstat().st_mode & 0o022 for item in walk(self.host.path(path))
        )

    def caches_ok(self) -> bool:
        if not self.host.path(CACHES).is_dir():
            return True
        return not any(
            self.host.owner(item)[0] != "root" or item.lstat().st_mode & 0o022
            for item in self.host.path(CACHES).iterdir()
            if fnmatch.fnmatch(item.name, "*.ext4")
        )

    def host_settings(self) -> None:
        h = self.host
        self.check(
            "KSM off", lambda: h.path("/sys/kernel/mm/ksm/run").read_text().strip("\n") == "0"
        )
        self.check(
            "token root-only", lambda: self.mode_is("/etc/homelab-ci/github-token", "root:root 600")
        )
        self.check(
            "settings dir root-only", lambda: self.mode_is("/etc/homelab-ci", "root:root 700")
        )
        self.check(
            "supervisor state dir root-only",
            lambda: (
                not h.path("/run/homelab-ci").exists()
                or self.mode_is("/run/homelab-ci", "root:root 700")
            ),
        )
        self.check(
            "no leftover job registrations",
            lambda: not any(h.path("/run/homelab-ci").glob("metadata-slot*.json")),
        )
        self.check(
            "image root-owned, read-only to others",
            lambda: self.mode_is(IMAGE, "root:root 644"),
        )
        self.check("cache folder root-owned", lambda: self.mode_is(CACHES, "root:root 755"))
        self.check("cache disks root-owned, read-only to others", self.caches_ok)
        for file in (
            LIB,
            "/etc/systemd/system/homelab-ci-net.service",
            "/etc/systemd/system/homelab-ci-supervisor.service",
            "/usr/local/bin/firecracker",
            "/usr/local/bin/jailer",
        ):
            self.check(
                f"only root can change {file}", lambda file=file: self.root_only_writes(file)
            )

    def slot_users(self) -> None:
        """What a slot user (Firecracker after a VM escape) can reach."""
        h = self.host
        for user in ("fc-slot0", "fc-slot1"):
            if not h.ok("id", user, quiet=True):
                self.fail(f"{user} exists")
                continue
            other = f"slot{1 - int(user.removeprefix('fc-slot'))}"
            for path in (
                "/etc/homelab-ci/github-token",
                "/run/homelab-ci",
                "/var/run/docker.sock",
                "/home/colin",
                "/home/colin/actions-runner",
                "/etc/shadow",
                "/root",
                f"/srv/jailer/firecracker/ci-{other}",
            ):
                self.check(
                    f"{user} cannot read {path}",
                    lambda user=user, path=path: not h.ok("sudo", "-u", user, "test", "-r", path),
                )
            self.check(
                f"{user} cannot write the image",
                lambda user=user: not h.ok("sudo", "-u", user, "test", "-w", IMAGE),
            )
            for cache in sorted(h.path(CACHES).glob("*.ext4")):
                real = f"{CACHES}/{cache.name}"
                self.check(
                    f"{user} cannot write {cache.name}",
                    lambda user=user, real=real: not h.ok("sudo", "-u", user, "test", "-w", real),
                )
            self.check(
                f"{user} has no login shell",
                lambda user=user: (
                    h.out("getent", "passwd", user).rstrip("\n").split(":")[6]
                    == "/usr/sbin/nologin"
                ),
            )
            self.check(
                f"{user} not in docker, sudo or kvm groups",
                lambda user=user: PRIVILEGED.search(h.out("id", "-nG", user)) is None,
            )
            self.check(
                f"{user} cannot open a connection (root can)",
                lambda user=user: (
                    h.ok("timeout", "3", "bash", "-c", CONNECT)
                    and not h.ok(
                        "sudo", "-u", user, "timeout", "3", "bash", "-c", CONNECT, quiet=True
                    )
                ),
            )
        slot_uids = h.out("nft", "list", "set", "inet", "homelab_ci", "slot_uids", quiet=True)
        for line in h.out("getent", "passwd").splitlines():
            fields = line.split(":")
            if len(fields) > 2 and SLOT_USER.fullmatch(fields[0]):
                word = re.compile(rf"(?<!\w){re.escape(fields[2])}(?!\w)")
                self.check(
                    f"{fields[0]} is in the network wall",
                    lambda word=word: word.search(slot_uids) is not None,
                )

    def image(self) -> None:
        """Nothing secret in the image."""
        h = self.host
        scan = Path(tempfile.mkdtemp())
        try:
            try:
                mounted = h.ok("mount", "-o", "ro,loop", IMAGE, str(scan))
            except FileNotFoundError:
                mounted = False
            if not mounted:
                self.fail("image could not be mounted for the secrets scan")
                return
            self.check(
                "no SSH host keys in image", lambda: not any((scan / "etc/ssh").glob("ssh_host_*"))
            )
            self.check("no SSH server in image", lambda: not (scan / "usr/sbin/sshd").exists())

            def no_secrets() -> bool:
                folders = [str(scan / name) for name in ("etc", "home", "root", "opt")]
                found = h.out("grep", "-rlIE", SECRETS, *folders, quiet=True).splitlines()
                return not [f for f in found if not f.startswith(f"{scan}/opt/actions-cache/")]

            self.check("no tokens or private keys in image", no_secrets)

            # Jobs install the Python versions a repo asks for next to the baked-in one.
            def runner_owns_python() -> bool:
                passwd = (scan / "etc/passwd").read_text().splitlines()
                uids = [
                    f[2] for line in passwd if len(f := line.split(":")) > 2 and f[0] == "runner"
                ]
                python = scan / "opt/uv-python"
                if not uids or not uids[-1] or not python.is_dir():
                    return False
                uid = int(uids[-1])
                return all(item.lstat().st_uid == uid for item in walk(python))

            self.check("runner user owns uv's Python folder", runner_owns_python)
            h.run(["umount", str(scan)])
        finally:
            # Left behind when umount failed; the audit carries on, as it did in shell.
            try:
                scan.rmdir()
            except OSError as e:
                print(f"rmdir: {e}", file=sys.stderr)

    def live_vm(self, run_vm: str) -> None:
        """A live VM: how Firecracker is confined, and what the guest is told."""
        h = self.host
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            metadata = tmp / "metadata.json"
            metadata.write_text(json.dumps({"task": "probe", "probe": MMDS_PROBE}))
            try:
                vm: Waitable | None = h.start(
                    [run_vm, "0", str(metadata), "90"], tmp / "console.log"
                )
            except OSError as e:
                # A missing run-vm.sh: no Firecracker to find, and the checks below fail.
                print(f"{run_vm}: {e}", file=sys.stderr)
                (tmp / "console.log").touch()
                vm = None
            pid = ""
            for _ in range(30):
                pid = next(iter(h.out("pgrep", "-x", "firecracker").splitlines()), "")
                if pid:
                    break
                h.sleep(1)
            h.sleep(5)
            if pid:
                self.firecracker(pid)
            else:
                self.fail("no Firecracker process to inspect")
            if vm:
                vm.wait()
            console = (tmp / "console.log").read_text(errors="replace")
            found = re.search(r"MMDS_KEYS .*", console)
            keys = found.group(0).replace("\r", "") if found else ""
            self.check(
                f"guest is told only its task ({keys})",
                lambda: keys == 'MMDS_KEYS ["probe","task"]',
            )

    def firecracker(self, pid: str) -> None:
        h = self.host
        proc = f"/proc/{pid}"
        status = h.path(f"{proc}/status")
        self.check(
            "Firecracker runs as fc-slot0", lambda: self.owner(proc).split(":")[0] == "fc-slot0"
        )
        for name, pattern in (
            ("Firecracker has a syscall filter", r"^Seccomp:\s+2"),
            ("Firecracker cannot gain privileges", r"^NoNewPrivs:\s+1"),
            ("Firecracker has no capabilities", r"^CapEff:\s+0+$"),
        ):
            self.check(
                name,
                lambda pattern=pattern: (
                    re.search(pattern, status.read_text(), re.MULTILINE) is not None
                ),
            )

        # The jailer pivots into its chroot, so the process's / is the chroot mount.
        def root_mount() -> str:
            mounts = [line.split() for line in h.path(f"{proc}/mountinfo").read_text().splitlines()]
            return "\n".join(m[3] for m in mounts if len(m) > 4 and m[4] == "/")

        self.check(
            "Firecracker is chrooted",
            lambda: root_mount() == "/srv/jailer/firecracker/ci-slot0/root",
        )
        self.check(
            "Firecracker sees only its own files",
            lambda: " ".join(sorted(os.listdir(h.path(f"{proc}/root")))) == JAIL_FILES,
        )
        cgroup = "/sys/fs/cgroup/firecracker"
        self.check(
            "memory capped",
            lambda: h.path(f"{cgroup}/ci-slot0/memory.max").read_text().strip("\n") != "max",
        )
        # The parent's weight ranks the VMs against system.slice (100), where the services run.
        self.check(
            "CPU weight below services",
            lambda: int(h.path(f"{cgroup}/cpu.weight").read_text()) < 100,
        )


def walk(top: Path) -> Iterator[Path]:
    """TOP and everything under it, not following links, as find does."""
    yield top
    for folder, folders, files in os.walk(top):
        for name in folders + files:
            yield Path(folder, name)


def main(host: Host | None = None, here: Path | None = None) -> int:
    host = host or Host()
    here = here or Path(__file__).resolve().parent
    if not host.is_root():
        print("run with sudo", file=sys.stderr)
        return 1
    # Its live-VM checks must inspect its own VM, not one running a job.
    if host.ok("systemctl", "is-active", "--quiet", "homelab-ci-supervisor.service") or host.ok(
        "pgrep", "-x", "firecracker"
    ):
        print("stop the supervisor and wait for running VMs first", file=sys.stderr)
        return 1
    run_vm = host.env.get("RUN_VM") or RUN_VM
    audit = Audit(host)
    audit.host_settings()
    audit.slot_users()
    audit.image()
    audit.live_vm(run_vm)
    # The network, from inside a VM.
    audit.check(
        "network test",
        lambda: (
            host.run(
                [sys.executable, "-Es", str(here / "test_network.py")],
                env={**host.env, "RUN_VM": run_vm},
            ).returncode
            == 0
        ),
    )
    if audit.failures:
        print(f"AUDIT FAILED: {audit.failures} checks")
        return 1
    print("audit passed")
    return 0


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
