#!/usr/bin/env python3
"""Prepare the Beelink to run CI jobs in Firecracker microVMs. Run from a checkout of main;
deploys never touch these files:

    sudo ci-runner/install-host.sh
    sudo SLOTS=4 ci-runner/install-host.sh   (a new slot count, written to /etc/homelab-ci)

update.py reruns it when main changes anything under ci-runner/ or scripts/. Runs as root with
the host's system Python: standard library only.
"""

from __future__ import annotations

import fcntl
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

HERE = Path(__file__).resolve().parent

SERVICE = "homelab-ci-supervisor.service"
DEST = "/usr/local/lib/homelab-ci"
SLOTS_FILE = "/etc/homelab-ci/slots"
DEFAULT_SLOTS = 5
RUN_DIR = "/run/homelab-ci"
REASON_FILE = f"{RUN_DIR}/restart-reason"
GH = "https://github.com/firecracker-microvm/firecracker/releases/download"


class Run(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        check: bool = True,
        capture: bool = False,
        quiet: bool = False,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


def run_command(
    command: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    quiet: bool = False,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command: `capture` keeps its stdout, `quiet` drops its stderr. A missing program
    fails as the shell's 127."""
    try:
        return subprocess.run(
            command,
            check=check,
            text=True,
            input=input,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.DEVNULL if quiet else None,
        )
    except FileNotFoundError:
        if check:
            raise
        return subprocess.CompletedProcess(command, 127, "", "")


@dataclass(frozen=True)
class Host:
    """Where the install writes (`root`, `/` but in tests) and how it runs commands."""

    root: Path = Path("/")
    run: Run = run_command

    def path(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")


def versions(text: str) -> dict[str, str]:
    """versions.env's NAME=value lines, as the shell reads them (as scripts/ci.py's `versions`,
    kept here so the installer imports nothing from the checkout)."""
    found: dict[str, str] = {}
    for token in shlex.split(text, comments=True):
        name, _, value = token.partition("=")
        if re.fullmatch(r"[A-Z0-9_]+", name):
            found[name] = value
    return found


def slot_count(env: Mapping[str, str], host: Host) -> int:
    """SLOTS if given, else the host's file from the last run: 5 when not a number, at least 1,
    as in ci_supervisor.py."""
    text = env.get("SLOTS") or ""
    if not text:
        try:
            text = host.path(SLOTS_FILE).read_text()
        except OSError:
            text = ""
    text = re.sub(r"\s", "", text)
    return max(1, int(text)) if re.fullmatch(r"[0-9]+", text) else DEFAULT_SLOTS


def differs(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() != b.read_bytes()
    except OSError:
        return True


def install(host: Host, mode: str, src: Path, dst: str, *, parents: bool = False) -> None:
    flags = ["-D"] if parents else []
    host.run(
        ["install", *flags, "-o", "root", "-g", "root", "-m", mode, str(src), str(host.path(dst))]
    )


def install_dirs(host: Host, mode: str, *dirs: str) -> None:
    host.run(
        ["install", "-d", "-o", "root", "-g", "root", "-m", mode, *map(str, map(host.path, dirs))]
    )


def systemctl(host: Host, *args: str) -> None:
    host.run(["systemctl", *args])


def active(host: Host) -> bool:
    return host.run(["systemctl", "is-active", "--quiet", SERVICE], check=False).returncode == 0


def has_version(host: Host, tool: str, name: str, version: str) -> bool:
    result = host.run(
        [str(host.path(f"/usr/local/bin/{tool}")), "--version"],
        check=False,
        capture=True,
        quiet=True,
    )
    return result.returncode == 0 and any(
        line.endswith(f"{name} {version}") for line in result.stdout.splitlines()
    )


def add_user(host: Host, name: str) -> None:
    if host.run(["id", name], check=False, capture=True, quiet=True).returncode:
        host.run(
            [
                "useradd",
                "--system",
                "--no-create-home",
                "--home-dir",
                "/nonexistent",
                "--shell",
                "/usr/sbin/nologin",
                name,
            ]
        )


def install_firecracker(host: Host, version: str, sha256: str) -> None:
    """Firecracker and jailer, pinned and checked against the hash in versions.env."""
    with tempfile.TemporaryDirectory() as tmp:
        tgz = f"{tmp}/firecracker-{version}-x86_64.tgz"
        host.run(["curl", "-fsSL", "-o", tgz, f"{GH}/{version}/firecracker-{version}-x86_64.tgz"])
        host.run(["sha256sum", "-c", "--quiet"], input=f"{sha256}  {tgz}\n")
        host.run(["tar", "-xzf", tgz, "-C", tmp])
        rel = Path(tmp) / f"release-{version}-x86_64"
        install(host, "0755", rel / f"firecracker-{version}-x86_64", "/usr/local/bin/firecracker")
        install(host, "0755", rel / f"jailer-{version}-x86_64", "/usr/local/bin/jailer")


def install_vm_files(host: Host, here: Path) -> None:
    """Files a VM reads when it starts. Without a restart they are swapped while jobs run: new VMs
    get them, running ones keep the copies they opened (install writes a new file, never the old
    one in place)."""
    install_dirs(host, "0755", DEST, f"{DEST}/net")
    # The Python first: a new wrapper runs only what is already there.
    for name in ("vm_disks.py", "run_vm.py", "stop_vms.py"):
        install(host, "0644", here / name, f"{DEST}/{name}")
    install(host, "0755", here / "run-vm.sh", f"{DEST}/run-vm.sh")
    install(host, "0755", here / "stop-vms.sh", f"{DEST}/stop-vms.sh")
    # After the new wrappers: a VM started before them runs the old run-vm.sh, which sources it.
    host.path(f"{DEST}/vm-disks.sh").unlink(missing_ok=True)
    # Before its wrapper, which the timer may start at any time.
    install(host, "0644", here / "update.py", f"{DEST}/update.py")
    install(host, "0755", here / "update.sh", f"{DEST}/update.sh")
    install(host, "0755", here / "tests" / "test_load.py", f"{DEST}/test_load.py")
    install(host, "0755", here / "tests" / "test-load.sh", f"{DEST}/test-load.sh")


def install_timed_script(host: Host, here: Path, script: str, unit: str) -> None:
    install(host, "0644", here.parent / "scripts" / script, f"{DEST}/{script}")
    for name in (f"{unit}.service", f"{unit}.timer"):
        install(host, "0644", here / "systemd" / name, f"/etc/systemd/system/{name}")
    systemctl(host, "daemon-reload")
    systemctl(host, "enable", "--now", f"{unit}.timer")


def install_host(host: Host, here: Path, env: Mapping[str, str]) -> None:
    pinned = versions((here / "versions.env").read_text(encoding="utf-8"))
    version = pinned["FIRECRACKER_VERSION"]
    slots = slot_count(env, host)

    # KSM merges identical memory pages across processes, which leaks data between
    # VMs through timing. Firecracker's production guide says off.
    host.path("/etc/tmpfiles.d/homelab-ci-ksm.conf").write_text(
        "w /sys/kernel/mm/ksm/run - - - - 0\n"
    )
    host.path("/sys/kernel/mm/ksm/run").write_text("0\n")

    if not has_version(host, "firecracker", "Firecracker", version) or not has_version(
        host, "jailer", "Jailer", version
    ):
        install_firecracker(host, version, pinned["FIRECRACKER_SHA256"])

    # One unprivileged user per slot; the jailer drops to it.
    for n in range(slots):
        add_user(host, f"fc-slot{n}")

    # One install at a time: a second waits its turn and is told whose is running.
    install_dirs(host, "0700", RUN_DIR)
    with host.path(f"{RUN_DIR}/install.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                whose = host.path(REASON_FILE).read_text().rstrip("\n")
            except OSError:
                whose = "reason unknown"
            say(f"Another install is running ({whose}); waiting for it.")
            fcntl.flock(lock, fcntl.LOCK_EX)
        reason = env.get("REASON") or f"{env.get('SUDO_USER') or 'root'}: install-host.sh"
        host.path(REASON_FILE).write_text(f"{reason}\n")
        supervisor = Supervisor()
        # However this ends, the reason goes and a supervisor that was running runs again.
        try:
            install_locked(host, here, slots, supervisor)
        finally:
            host.path(REASON_FILE).unlink(missing_ok=True)
            if supervisor.was_running and not active(host):
                host.run(["systemctl", "start", SERVICE], check=False)


@dataclass
class Supervisor:
    was_running: bool = False


def install_locked(host: Host, here: Path, slots: int, supervisor: Supervisor) -> None:
    dest = host.path(DEST)
    # Cache disks the supervisor builds from main-only outboxes; VMs read them, never write.
    # Outboxes are hard-linked into the jails (run_vm.py), so they share their filesystem.
    install_dirs(host, "0755", "/var/lib/homelab-ci/caches")
    install_dirs(host, "0700", "/var/lib/homelab-ci/outbox")
    # The SSD is trimmed hourly (the reason is in the file). Restarted only when it changes,
    # as a restart can run a trim missed under the weekly schedule at once.
    trim = "/etc/systemd/system/fstrim.timer.d/hourly.conf"
    if differs(here / "systemd" / "fstrim-hourly.conf", host.path(trim)):
        install(host, "0644", here / "systemd" / "fstrim-hourly.conf", trim, parents=True)
        systemctl(host, "daemon-reload")
        systemctl(host, "restart", "fstrim.timer")
    # Alloy's textfile metrics. Root owns it, as the backup container's root has no right to
    # write into another user's folder (cap_drop ALL); sticky and open to all, as Colin's user
    # timers (PC backups) write here too.
    install_dirs(host, "1777", "/var/lib/homelab-metrics")
    install_dirs(host, "0755", DEST)
    # The SSD's lifetime writes and wear, for Prometheus.
    install_timed_script(host, here, "ssd_wear.py", "homelab-ssd-wear")
    # What the intrusion alerts read, as a user of its own in the journal's group.
    add_user(host, "homelab-facts")
    install_timed_script(host, here, "security_facts.py", "homelab-security-facts")
    # CI and deploy run times, for the trace store and Prometheus.
    install_timed_script(host, here, "ci_times.py", "homelab-ci-times")

    # The supervisor, the network rules and the slot count take a restart. Running VMs carry
    # on through it (each is a unit of its own, taken back by the new supervisor), and the
    # rules reload in one transaction, so a restart takes seconds and cuts off nothing.
    restart_files = {
        here.parent / "scripts" / "ci_supervisor.py": f"{DEST}/ci_supervisor.py",
        here / "net" / "homelab-ci.nft": f"{DEST}/net/homelab-ci.nft",
        here / "net" / "setup_net.py": f"{DEST}/net/setup_net.py",
        here / "net" / "setup-net.sh": f"{DEST}/net/setup-net.sh",
        here / "systemd" / "homelab-ci-net.service": "/etc/systemd/system/homelab-ci-net.service",
        here / "systemd" / SERVICE: f"/etc/systemd/system/{SERVICE}",
    }
    restart = any(differs(src, host.path(dst)) for src, dst in restart_files.items())
    try:
        restart |= host.path(SLOTS_FILE).read_text().rstrip("\n") != str(slots)
    except OSError:
        restart = True
    restart |= not active(host)

    if not restart:
        install_vm_files(host, here)
        how = "installed while jobs ran, no restart needed"
    else:
        if active(host):
            supervisor.was_running = True
            # A supervisor from before VMs ran as their own units owns its VMs: stopping it
            # waits for their jobs (up to 20 minutes) instead of cutting them off.
            try:
                old = (dest / "ci_supervisor.py").read_text()
            except OSError:
                old = ""
            if "UNIT_PREFIX" not in old:
                say(
                    "The running supervisor owns its VMs: waiting for their jobs to finish "
                    "(up to 20 minutes)."
                )
            systemctl(host, "stop", SERVICE)

        # Host-side settings, never in the repo: the slot count, and the GitHub token
        # (written by hand, see docs/architecture.md, "CI runner"). Which repos are served is
        # the token's repository access. Written before the network, which reads the slot count.
        install_dirs(host, "0700", "/etc/homelab-ci")
        host.path(SLOTS_FILE).write_text(f"{slots}\n")

        install_vm_files(host, here)
        install(host, "0644", here / "net" / "homelab-ci.nft", f"{DEST}/net/homelab-ci.nft")
        # The code before its wrapper: run-vm.sh calls setup-net.sh at any time.
        install(host, "0755", here / "net" / "setup_net.py", f"{DEST}/net/setup_net.py")
        install(host, "0755", here / "net" / "setup-net.sh", f"{DEST}/net/setup-net.sh")
        install(
            host, "0755", here.parent / "scripts" / "ci_supervisor.py", f"{DEST}/ci_supervisor.py"
        )
        for unit in ("homelab-ci-net.service", SERVICE):
            install(host, "0644", here / "systemd" / unit, f"/etc/systemd/system/{unit}")
        systemctl(host, "daemon-reload")
        systemctl(host, "enable", "--now", "homelab-ci-net.service")
        # Apply rule changes in place; restarting the unit would remove the taps.
        host.run([str(dest / "net" / "setup-net.sh"), "up"])
        if supervisor.was_running:
            systemctl(host, "enable", "--now", SERVICE)
            how = "network up, supervisor restarted"
        else:
            how = "network up, supervisor not running"

    # The Beelink takes main on its own (update.sh --follow, every minute).
    for unit in ("homelab-ci-update.service", "homelab-ci-update.timer"):
        install(host, "0644", here / "systemd" / unit, f"/etc/systemd/system/{unit}")
    # The Beelink's load test, which update.py starts when its slot count has no result
    # (docs/architecture.md, "CI runner").
    unit = "homelab-ci-load-test.service"
    install(host, "0644", here / "systemd" / unit, f"/etc/systemd/system/{unit}")
    systemctl(host, "daemon-reload")
    systemctl(host, "enable", "--now", "homelab-ci-update.timer")

    firecracker = (
        host.run(
            [str(host.path("/usr/local/bin/firecracker")), "--version"], check=False, capture=True
        )
        .stdout.split("\n", 1)[0]
        .split()
    )
    say(f"Firecracker {firecracker[1] if len(firecracker) > 1 else ''}, {slots} slots, {how}.")


def say(text: str) -> None:
    # Flushed at once, so the journal keeps it in order with the commands' own output.
    print(text, flush=True)


def main() -> int:
    if os.geteuid() != 0:
        print("run with sudo", file=sys.stderr)
        return 1
    # As bash's EXIT trap: a stopped install still restarts the supervisor it stopped.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    try:
        install_host(Host(), HERE, os.environ)
    except subprocess.CalledProcessError as e:
        return e.returncode if e.returncode > 0 else 128 - e.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
