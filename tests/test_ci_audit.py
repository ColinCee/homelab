"""The CI runner's audit (ci-runner/tests/audit.py): every check runs, and any one failing
fails the audit, on a fake host that passes them all unless a test breaks one."""

import importlib.util
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "audit_script", Path(__file__).parents[1] / "ci-runner/tests/audit.py"
)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
sys.modules["audit_script"] = audit
SPEC.loader.exec_module(audit)

PID = "4242"
JAIL = audit.JAIL_FILES.split()
# Host settings 13, each of two slot users 13, the wall 1, the image 4, the live VM 9, the
# network test 1: as many as audit.sh ran on a host with one cache disk and one slot user.
CHECKS = 54


class VM:
    def wait(self) -> int:
        return 0


class FakeHost:
    """Answers the audit's commands as a sound host would; `answers` overrides one."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.vm_started = False
        self.boots = True
        self.answers: dict[tuple[str, ...], int] = {}
        self.ran: list[list[str]] = []

    def run(
        self, command: list[str], *, env: Mapping[str, str] | None = None, quiet: bool = False
    ) -> subprocess.CompletedProcess[str]:
        self.ran.append(command)
        code, out = self.answer(command)
        code = self.answers.get(tuple(command), code)
        return subprocess.CompletedProcess(command, code, out)

    def answer(self, command: list[str]) -> tuple[int, str]:
        match command:
            case ["systemctl", "is-active", "--quiet", _]:
                return 3, ""
            case ["pgrep", "-x", "firecracker"]:
                return (0, f"{PID}\n") if self.vm_started else (1, "")
            case ["id", user] if user.startswith("fc-slot"):
                return 0, ""
            case ["sudo", "-u", _, *_]:
                return 1, ""
            case ["getent", "passwd", user]:
                return 0, f"{user}:x:990:990::/nonexistent:/usr/sbin/nologin\n"
            case ["getent", "passwd"]:
                return 0, "root:x:0:0::/root:/bin/bash\nfc-slot0:x:990:990::/:/x\n"
            case ["id", "-nG", user]:
                return 0, f"{user}\n"
            case ["timeout", "3", "bash", "-c", audit.CONNECT]:
                return 0, ""
            case ["nft", "list", "set", "inet", "homelab_ci", "slot_uids"]:
                return 0, "set slot_uids { elements = { 990, 991 } }\n"
            case ["mount", "-o", "ro,loop", _, scan]:
                (Path(scan) / "etc").mkdir()
                (Path(scan) / "etc/passwd").write_text(f"runner:x:{os.getuid()}:1000::/:/bin/sh\n")
                (Path(scan) / "opt/uv-python/cpython").mkdir(parents=True)
                return 0, ""
            case ["grep", "-rlIE", *_]:
                return 1, ""
            case ["umount", scan]:
                for path in sorted(Path(scan).rglob("*"), reverse=True):
                    path.rmdir() if path.is_dir() else path.unlink()
                return 0, ""
            case [_, network] if network.endswith("test_network.py"):
                return 0, ""
        raise AssertionError(f"unexpected command {command}")

    def start(self, command: list[str], log: Path) -> VM:
        self.vm_started = self.boots
        log.write_text('booting\nMMDS_KEYS ["probe","task"]\r\n' if self.boots else "")
        return VM()


def write(root: Path, path: str, mode: int, text: str = "") -> Path:
    file = root / path.lstrip("/")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)
    file.chmod(mode)
    return file


def folder(root: Path, path: str, mode: int) -> Path:
    made = root / path.lstrip("/")
    made.mkdir(parents=True, exist_ok=True)
    made.chmod(mode)
    return made


@pytest.fixture
def host(tmp_path: Path) -> FakeHost:
    root = tmp_path
    write(root, "/sys/kernel/mm/ksm/run", 0o644, "0\n")
    folder(root, "/etc/homelab-ci", 0o700)
    write(root, "/etc/homelab-ci/github-token", 0o600, "token")
    folder(root, "/run/homelab-ci", 0o700)
    write(root, audit.IMAGE, 0o644)
    folder(root, audit.CACHES, 0o755)
    write(root, f"{audit.CACHES}/o__r.ext4", 0o644)
    write(root, f"{audit.LIB}/run-vm.sh", 0o755)
    folder(root, audit.LIB, 0o755)
    for unit in ("homelab-ci-net.service", "homelab-ci-supervisor.service"):
        write(root, f"/etc/systemd/system/{unit}", 0o644)
    for program in ("firecracker", "jailer"):
        write(root, f"/usr/local/bin/{program}", 0o755)
    status = "Name:\tfirecracker\nSeccomp:\t2\nNoNewPrivs:\t1\nCapEff:\t0000000000000000\n"
    write(root, f"/proc/{PID}/status", 0o644, status)
    mountinfo = "1 0 8:1 /srv/jailer/firecracker/ci-slot0/root / rw - ext4 /dev/sda1 rw\n"
    write(root, f"/proc/{PID}/mountinfo", 0o444, mountinfo)
    for name in JAIL:
        folder(root, f"/proc/{PID}/root/{name}", 0o755)
    write(root, "/sys/fs/cgroup/firecracker/cpu.weight", 0o644, "20\n")
    write(root, "/sys/fs/cgroup/firecracker/ci-slot0/memory.max", 0o644, "2147483648\n")
    return FakeHost(root)


def owners(slot_owned: set[Path]) -> Callable[[Path], tuple[str, str]]:
    return lambda path: ("fc-slot0", "fc-slot0") if path in slot_owned else ("root", "root")


def run_audit(host: FakeHost, *, owner: Callable[[Path], tuple[str, str]] | None = None) -> int:
    proc = host.root / "proc" / PID
    fake = audit.Host(
        run=host.run,
        start=host.start,
        root=host.root,
        sleep=lambda seconds: None,
        env={},
        is_root=lambda: True,
        owner=owner or owners({proc}),
    )
    return int(audit.main(fake, here=Path("/repo/ci-runner/tests")))


def test_a_sound_host_passes_every_check(
    host: FakeHost, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_audit(host) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1] == "audit passed"
    assert not [line for line in lines[:-1] if not line.startswith("PASS ")]
    names = {line.removeprefix("PASS ") for line in lines[:-1]}
    assert {
        "KSM off",
        "token root-only",
        "only root can change /usr/local/lib/homelab-ci",
        "fc-slot1 cannot read /srv/jailer/firecracker/ci-slot0",
        "fc-slot0 cannot write o__r.ext4",
        "fc-slot0 is in the network wall",
        "runner user owns uv's Python folder",
        "Firecracker runs as fc-slot0",
        "Firecracker sees only its own files",
        "CPU weight below services",
        'guest is told only its task (MMDS_KEYS ["probe","task"])',
        "network test",
    } <= names
    assert len(names) == len(lines) - 1 == CHECKS


BREAKS: dict[str, Callable[[FakeHost], object]] = {
    "KSM off": lambda h: (h.root / "sys/kernel/mm/ksm/run").write_text("1\n"),
    "token root-only": lambda h: (h.root / "etc/homelab-ci/github-token").chmod(0o640),
    "cache disks root-owned, read-only to others": lambda h: (
        h.root / audit.CACHES.lstrip("/") / "o__r.ext4"
    ).chmod(0o666),
    "only root can change /usr/local/lib/homelab-ci": lambda h: (
        h.root / audit.LIB.lstrip("/") / "run-vm.sh"
    ).chmod(0o775),
    "fc-slot0 cannot read /etc/homelab-ci/github-token": lambda h: h.answers.update(
        {("sudo", "-u", "fc-slot0", "test", "-r", "/etc/homelab-ci/github-token"): 0}
    ),
    "fc-slot1 cannot open a connection (root can)": lambda h: h.answers.update(
        {("sudo", "-u", "fc-slot1", "timeout", "3", "bash", "-c", audit.CONNECT): 0}
    ),
    "no leftover job registrations": lambda h: (
        h.root / "run/homelab-ci/metadata-slot0.json"
    ).write_text("{}"),
    "Firecracker has no capabilities": lambda h: (h.root / f"proc/{PID}/status").write_text(
        "Seccomp:\t2\nNoNewPrivs:\t1\nCapEff:\t00000000a80425fb\n"
    ),
    "Firecracker sees only its own files": lambda h: (h.root / f"proc/{PID}/root/home").mkdir(),
    "memory capped": lambda h: (
        h.root / "sys/fs/cgroup/firecracker/ci-slot0/memory.max"
    ).write_text("max\n"),
    "network test": lambda h: h.answers.update(
        {(sys.executable, "/repo/ci-runner/tests/test_network.py"): 1}
    ),
}


@pytest.mark.parametrize("name", BREAKS)
def test_any_one_failing_check_fails_the_audit_and_the_rest_still_run(
    host: FakeHost, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    BREAKS[name](host)
    assert run_audit(host) == 1
    lines = capsys.readouterr().out.splitlines()
    assert [line for line in lines if line.startswith("FAIL ")] == [f"FAIL {name}"]
    assert lines[-1] == "AUDIT FAILED: 1 checks"
    assert len(lines) == CHECKS + 1  # every check still ran


def test_a_missing_file_fails_its_check_instead_of_stopping_the_audit(
    host: FakeHost, capsys: pytest.CaptureFixture[str]
) -> None:
    (host.root / "sys/fs/cgroup/firecracker/cpu.weight").unlink()
    assert run_audit(host) == 1
    out = capsys.readouterr().out
    assert "FAIL CPU weight below services" in out and "PASS network test" in out


def test_a_missing_mount_program_fails_the_image_check_instead_of_crashing(
    host: FakeHost, capsys: pytest.CaptureFixture[str]
) -> None:
    class NoMount(FakeHost):
        def answer(self, command: list[str]) -> tuple[int, str]:
            if command[0] == "mount":
                raise FileNotFoundError("mount")
            return super().answer(command)

    no_mount = NoMount(host.root)
    assert run_audit(no_mount) == 1
    out = capsys.readouterr().out
    assert "FAIL image could not be mounted for the secrets scan" in out
    assert "PASS network test" in out


def test_a_firecracker_not_run_by_its_slot_user_fails(
    host: FakeHost, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_audit(host, owner=owners(set())) == 1
    assert "FAIL Firecracker runs as fc-slot0" in capsys.readouterr().out


def test_a_missing_slot_user_fails_and_skips_its_checks(
    host: FakeHost, capsys: pytest.CaptureFixture[str]
) -> None:
    host.answers[("id", "fc-slot1")] = 1
    assert run_audit(host) == 1
    out = capsys.readouterr().out
    assert "FAIL fc-slot1 exists" in out and "fc-slot1 cannot read" not in out


def test_no_firecracker_process_fails(host: FakeHost, capsys: pytest.CaptureFixture[str]) -> None:
    host.boots = False
    assert run_audit(host) == 1
    out = capsys.readouterr().out
    assert "FAIL no Firecracker process to inspect" in out
    assert "FAIL guest is told only its task ()" in out


def test_it_will_not_run_beside_the_supervisor(
    host: FakeHost, capsys: pytest.CaptureFixture[str]
) -> None:
    host.answers[("systemctl", "is-active", "--quiet", "homelab-ci-supervisor.service")] = 0
    assert run_audit(host) == 1
    assert "stop the supervisor" in capsys.readouterr().err
    assert host.vm_started is False
