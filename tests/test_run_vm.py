"""A CI VM's boot path (ci-runner/run_vm.py, vm_disks.py, stop_vms.py), through its seam: every
command is recorded, and files go under tmp_path."""

import dataclasses
import fcntl
import json
import os
import signal
import stat
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
import run_vm
import stop_vms
import vm_disks
from vm_disks import Host

from scripts import ci_supervisor as sup

Answer = tuple[int, str]


class Recorder:
    """Answers each command by its longest matching start; anything else succeeds silently."""

    def __init__(self, answers: dict[str, Answer], hooks: dict[str, Callable[[], None]]) -> None:
        self.answers = answers
        self.hooks = hooks
        self.calls: list[list[str]] = []

    def __call__(
        self, command: Sequence[str], *, out: bool = False, quiet: bool = False
    ) -> subprocess.CompletedProcess[str]:
        cmd = list(command)
        self.calls.append(cmd)
        line = " ".join(cmd)
        if cmd[0] == "mknod":
            Path(cmd[3]).touch()
        for start, hook in self.hooks.items():
            if line.startswith(start):
                hook()
        starts = [start for start in self.answers if line.startswith(start)]
        code, stdout = self.answers[max(starts, key=len)] if starts else (0, "")
        return subprocess.CompletedProcess(cmd, code, stdout)

    def ran(self, start: str) -> list[list[str]]:
        return [c for c in self.calls if " ".join(c).startswith(start)]


class Jailer:
    """The jailer as the run sees it: it ends by itself with `code`, or, when the box hangs it,
    only once it is killed."""

    def __init__(self, box: Box, code: int) -> None:
        self.box = box
        self.code = code

    def killed(self) -> bool:
        box = self.box
        return (
            (box.cgroup / "cgroup.kill").exists()
            or signal.SIGKILL in box.group_signals
            or (signal.SIGTERM in box.group_signals and not box.survives_sigterm)
        )

    def wait(self, timeout: float) -> int:
        if self.box.on_wait:
            self.box.on_wait()
        if self.box.hang and not self.killed():
            raise subprocess.TimeoutExpired("jailer", timeout)
        return self.code

    def kill_group(self, signum: int) -> None:
        self.box.group_signals.append(signum)


class Box:
    """A host for slot 3 under tmp_path, with images, a metadata file and a recorder."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "root"
        images = self.root / "var/lib/homelab-ci/images"
        images.mkdir(parents=True)
        (images / "rootfs.ext4").write_bytes(b"r" * 1000)
        (images / "vmlinuz").write_bytes(b"kernel")
        self.metadata = self.root / "run/homelab-ci/metadata-slot3.json"
        self.metadata.parent.mkdir(parents=True)
        self.metadata.write_text('{"task": "runner"}')
        self.chroot = self.root / "srv/jailer/firecracker/ci-slot3/root"
        self.seen: dict[str, str | int] = {}
        self.answers: dict[str, Answer] = {
            "id -u fc-slot3": (0, f"{os.getuid()}\n"),
            "id -g fc-slot3": (0, f"{os.getgid()}\n"),
            "dmsetup info ci-slot": (1, ""),  # no snapshot left over
            "losetup --find --show --read-only": (0, "/dev/loop1\n"),
            "losetup --find --show": (0, "/dev/loop2\n"),
            "blockdev --getsz": (0, "8\n"),
            "dmsetup info -c --noheadings -o major,minor ci-slot3-root": (0, "253:7\n"),
            "dmsetup info -c --noheadings -o major,minor ci-slot3-cache": (0, "253:8\n"),
        }
        self.cgroup = self.root / "sys/fs/cgroup/firecracker/ci-slot3"
        self.hang = False
        self.survives_sigterm = False
        self.on_wait: Callable[[], None] | None = None
        self.group_signals: list[int] = []
        self.run = Recorder(self.answers, {"timeout ": self.look})
        self.host = Host(
            run=self.run,
            spawn=self.spawn,
            root=self.root,
            sleep=lambda _: None,
            root_ids=(os.getuid(), os.getgid()),
        )

    def spawn(self, command: Sequence[str]) -> Jailer:
        return Jailer(self, self.run(command).returncode)

    def look(self) -> None:
        """What the jailer would find, while it runs."""
        self.seen["vm.json"] = (self.chroot / "vm.json").read_text()
        for name in ("vm.json", "metadata.json", "vmlinuz", "rootfs.ext4"):
            self.seen[name + " mode"] = stat.S_IMODE((self.chroot / name).stat().st_mode)
        self.seen["chroot mode"] = stat.S_IMODE(self.chroot.stat().st_mode)

    def main(self, env: dict[str, str] | None = None, signals: run_vm.Signals | None = None) -> int:
        return run_vm.main(
            ["3", str(self.metadata), "600"], env or {}, self.host, signals or run_vm.Signals()
        )


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def test_a_slot_boots_its_snapshot_under_the_jailer(box: Box) -> None:
    assert box.main({"VCPUS": "4", "MEM_MIB": "4096"}) == 0

    assert box.run.ran("losetup --find")[0][-1].endswith("rootfs.ext4")
    assert box.run.ran("dmsetup create") == [
        ["dmsetup", "create", "ci-slot3-root", "--table", "0 8 snapshot /dev/loop1 /dev/loop2 N 8"]
    ]
    assert ["losetup", "-d", "/dev/loop1", "/dev/loop2"] in box.run.calls
    assert box.run.ran("mknod") == [
        ["mknod", "-m", "0600", str(box.chroot / "rootfs.ext4"), "b", "253", "7"]
    ]
    assert box.run.ran("timeout") == [
        ["timeout", "--kill-after=10", "600", "/usr/local/bin/jailer",
         "--id", "ci-slot3", "--exec-file", "/usr/local/bin/firecracker",
         "--uid", str(os.getuid()), "--gid", str(os.getgid()),
         "--chroot-base-dir", str(box.root / "srv/jailer"),
         "--cgroup-version", "2", "--cgroup", "cpu.weight=20",
         "--cgroup", f"memory.max={(4096 + 256) * 1048576}", "--cgroup", "memory.swap.max=0",
         "--", "--config-file", "vm.json", "--no-api", "--metadata", "metadata.json",
         "--level", "Warning"]
    ]  # fmt: skip
    config = json.loads(str(box.seen["vm.json"]))
    assert config["machine-config"] == {"vcpu_count": 4, "mem_size_mib": 4096}
    assert config["network-interfaces"][0]["guest_mac"] == "06:00:0a:c8:03:02"
    assert config["boot-source"]["boot_args"].endswith("ci.ip=10.200.3.2/30 ci.gw=10.200.3.1")
    assert [d["drive_id"] for d in config["drives"]] == ["rootfs"]
    assert box.seen["chroot mode"] == 0o700
    for name in ("vm.json", "metadata.json", "vmlinuz", "rootfs.ext4"):
        assert box.seen[name + " mode"] == 0o600, name
    # Everything of the run's goes with it: the jail, the snapshots and the registration.
    assert not (box.root / "srv/jailer/firecracker/ci-slot3").exists()
    assert not box.metadata.exists()
    assert box.run.ran("dmsetup ls")


def test_the_cache_and_outbox_disks_come_from_the_supervisors_env(box: Box, tmp_path: Path) -> None:
    cache = tmp_path / "cache.ext4"
    cache.write_bytes(b"c" * 600)
    outbox = tmp_path / "outbox.tar"

    assert box.main({"CACHE_IMAGE": str(cache), "CACHE_OUT": str(outbox)}) == 0

    drives = json.loads(str(box.seen["vm.json"]))["drives"]
    assert [(d["drive_id"], d["path_on_host"]) for d in drives] == [
        ("rootfs", "rootfs.ext4"),
        ("cache", "cache.ext4"),
        ("outbox", "outbox.img"),
    ]
    create = box.run.ran("dmsetup create ci-slot3-cache")
    assert create and create[0][-1] == "0 8 snapshot /dev/loop1 /dev/loop2 N 8"
    # The outbox outlives the VM, marked empty, as large as the largest tar the supervisor takes.
    with outbox.open("rb") as f:
        data = f.read(4096)
    assert data.startswith(b"homelab-ci-outbox\n") and not data[18:4096].strip(b"\0")
    assert outbox.stat().st_size == run_vm.OUTBOX_BYTES >= sup.CACHE_MAX_UNPACKED_BYTES + 512
    assert stat.S_IMODE(outbox.stat().st_mode) == 0o600


def test_a_missing_cache_image_or_unlinkable_outbox_leaves_the_disk_out(
    box: Box, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    outbox = tmp_path / "outbox.tar"

    def cross_device(*_: object) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(run_vm.os, "link", cross_device)
    env = {"CACHE_IMAGE": str(tmp_path / "gone.ext4"), "CACHE_OUT": str(outbox)}

    assert box.main(env) == 0

    assert [d["drive_id"] for d in json.loads(str(box.seen["vm.json"]))["drives"]] == ["rootfs"]
    assert not outbox.exists()
    err = capsys.readouterr().err
    assert f"slot 3: no outbox, {outbox} cannot be linked into the chroot" in err


@pytest.mark.parametrize(
    ("failing", "answer", "status", "message"),
    [
        ("dmsetup info ci-slot3-root", (0, ""), 1, "ci-slot3-root is still there"),
        ("losetup --find --show --read-only", (4, ""), 4, ""),
        ("losetup --find --show", (1, ""), 1, ""),
        ("blockdev --getsz", (5, ""), 5, ""),
        ("dmsetup create", (1, ""), 1, ""),
        ("mknod", (1, ""), 1, ""),
        ("timeout", (124, ""), 124, "VM ended (exit 124)"),
    ],
)
def test_a_failure_at_any_stage_cleans_up_and_says_how_it_ended(
    box: Box,
    capsys: pytest.CaptureFixture[str],
    failing: str,
    answer: Answer,
    status: int,
    message: str,
) -> None:
    box.answers[failing] = answer

    assert box.main() == status

    assert message in capsys.readouterr().err
    assert not (box.root / "srv/jailer/firecracker/ci-slot3").exists()
    assert not box.metadata.exists()
    # Once at the start and once at the end.
    assert len(box.run.ran("dmsetup ls")) == 2
    loops_freed = [c for c in box.run.ran("losetup -d") if "/dev/loop1" in c]
    if failing in ("losetup --find --show", "blockdev --getsz", "dmsetup create", "mknod"):
        assert loops_freed
    if failing != "timeout":
        assert not box.run.ran("timeout")


@pytest.mark.parametrize(
    ("failing", "status", "message"),
    [
        (str(run_vm.HERE / "net" / "setup-net.sh"), 1, "public address not blocked"),
        ("nft", 1, "fc-slot3 is not in the network wall; run setup-net.sh up"),
    ],
)
def test_without_the_network_wall_no_vm_starts_and_nothing_is_cleared(
    box: Box, capsys: pytest.CaptureFixture[str], failing: str, status: int, message: str
) -> None:
    box.answers[failing] = (1, "")

    assert box.main() == status

    assert message in capsys.readouterr().err
    assert box.metadata.exists()
    assert not box.run.ran("dmsetup")


def test_a_busy_slot_exits_75_and_leaves_the_running_vm_alone(
    box: Box, capsys: pytest.CaptureFixture[str]
) -> None:
    lock_dir = box.root / "run/homelab-ci"
    with (lock_dir / "slot3.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert box.main() == 75

    assert "slot 3 is busy" in capsys.readouterr().err
    assert box.metadata.exists()
    assert not box.run.ran("dmsetup")


def test_bad_guest_args_stop_the_boot(box: Box) -> None:
    args = box.root / "etc/homelab-ci/guest-args"
    args.parent.mkdir(parents=True)
    args.write_text("init=/bin/sh\n")

    assert box.main() == 2
    assert not box.run.ran("timeout")


def test_guest_args_are_appended_to_the_boot_line(box: Box) -> None:
    args = box.root / "etc/homelab-ci/guest-args"
    args.parent.mkdir(parents=True)
    args.write_text("mitigations=off\nfoo.bar=1\n")

    assert box.main() == 0
    boot = json.loads(str(box.seen["vm.json"]))["boot-source"]["boot_args"]
    assert boot.endswith("ci.gw=10.200.3.1 mitigations=off foo.bar=1")


def stop_during_the_run(box: Box, *, cgroup: bool = True) -> run_vm.Signals:
    """A VM that runs until killed, and a stop signal that arrives while it runs. Its cgroup is
    made once the run is under way: the run clears any left from before."""
    signals = run_vm.Signals()
    box.hang = True

    def stop() -> None:
        if cgroup:
            box.cgroup.mkdir(parents=True, exist_ok=True)
        signals.catch(signal.SIGTERM, None)

    box.on_wait = stop
    return signals


def test_a_stop_signal_kills_the_running_vm_at_once_and_cleans_up(
    box: Box, capsys: pytest.CaptureFixture[str]
) -> None:
    signals = stop_during_the_run(box)

    assert box.main(signals=signals) == 128 + signal.SIGTERM

    assert "VM ended (stopped)" in capsys.readouterr().err
    assert box.group_signals == []
    assert not box.metadata.exists()
    assert not (box.root / "srv/jailer/firecracker/ci-slot3").exists()
    assert (box.cgroup / "cgroup.kill").read_text() == "1\n"


def test_a_vm_that_survives_its_cgroup_kill_gets_sigterm_then_sigkill(box: Box) -> None:
    signals = stop_during_the_run(box, cgroup=False)  # nothing to write the kill to
    box.survives_sigterm = True

    assert box.main(signals=signals) == 128 + signal.SIGTERM

    assert box.group_signals == [signal.SIGTERM, signal.SIGKILL]
    assert not (box.root / "srv/jailer/firecracker/ci-slot3").exists()


@pytest.mark.parametrize("name", ["VCPUS", "MEM_MIB"])
@pytest.mark.parametrize("value", ["two", "0", "-1", "2.5"])
def test_a_bad_vcpus_or_memory_stops_the_boot(box: Box, name: str, value: str) -> None:
    assert box.main({name: value}) == 2
    assert not box.run.ran("dmsetup create")
    assert box.metadata.exists()


def test_a_signal_between_the_loops_and_their_mark_leaves_none_behind(box: Box) -> None:
    signals = run_vm.Signals()
    box.host = dataclasses.replace(box.host, run=signals.checked(box.run))
    box.run.hooks["dmsetup create"] = lambda: signals.catch(signal.SIGTERM, None)
    node = box.root / "node"

    with pytest.raises(vm_disks.Failed) as stopped:
        vm_disks.attach_disk(
            box.host, 3, "root", box.root / "var/lib/homelab-ci/images/vmlinuz", node
        )

    assert stopped.value.status == 128 + signal.SIGTERM
    assert ["dmsetup", "remove", "--retry", "ci-slot3-root"] in box.run.calls
    assert ["losetup", "-d", "/dev/loop1", "/dev/loop2"] in box.run.calls
    assert list(box.host.cow_dir.iterdir()) == []


def test_a_marked_snapshot_is_removed_not_its_loops_by_name(box: Box) -> None:
    box.answers["mknod"] = (1, "")
    image = box.root / "var/lib/homelab-ci/images/vmlinuz"

    with pytest.raises(vm_disks.Failed):
        vm_disks.attach_disk(box.host, 3, "root", image, box.root / "node")

    assert box.run.ran("dmsetup remove --retry ci-slot3-root")
    # Once, to mark them: the numbers may be another slot's by the time the snapshot is gone.
    assert len(box.run.ran("losetup -d")) == 1


def test_the_slots_files_go_even_if_the_end_line_cannot_be_printed(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    jail = box.root / "srv/jailer/firecracker/ci-slot3"
    jail.mkdir(parents=True)

    def broken(*_: object, **__: object) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(run_vm, "end_line", broken)

    with pytest.raises(BrokenPipeError):
        run_vm.cleanup(run_vm.Slot(3, box.host), run_vm.End("exit 0", 0, 2048, ""))

    assert box.run.ran("dmsetup ls")
    assert not jail.exists()


def end_line_host(tmp_path: Path, stat_text: str | None) -> Host:
    """Slot 3's root snapshot on dm-7; slot 13's on dm-9, which must not count."""
    sys_block = tmp_path / "sys/block"
    (sys_block / "dm-7").mkdir(parents=True)
    (sys_block / "dm-9").mkdir()
    if stat_text is not None:
        (sys_block / "dm-7" / "stat").write_text(stat_text)
    (sys_block / "dm-9" / "stat").write_text("0 0 0 0 0 0 999999 0 0 0 0\n")
    listing = (0, "ci-slot3-root dm-7\nci-slot13-root dm-9\n")
    return Host(run=Recorder({"dmsetup info -c": listing}, {}), root=tmp_path)


def test_the_end_line_gives_memory_peak_and_bytes_written(tmp_path: Path) -> None:
    host = end_line_host(tmp_path, "  100  0  800  5  2000  0  409600  90  0  50  95\n")
    outbox = tmp_path / "outbox.img"
    outbox.write_bytes(b"x" * 3 * 1048576)
    end = run_vm.End("exit 0", 300 * 1048576, 2048, str(outbox))

    # 409600 sectors written to the snapshot is 200 MiB; the outbox adds its 3 MiB of blocks.
    assert run_vm.end_line(host, 3, end) == (
        "slot 3: VM ended (exit 0), its memory peaked at 300 MiB of 2048, it wrote 203 MiB"
    )


def test_an_unreadable_counter_gives_a_question_mark(tmp_path: Path) -> None:
    end = run_vm.End("exit 0", 300 * 1048576, 2048, "")

    line = run_vm.end_line(end_line_host(tmp_path, None), 3, end)

    assert line.endswith("its memory peaked at 300 MiB of 2048, it wrote ? MiB")


def detach_box(tmp_path: Path) -> tuple[Host, Recorder, Path]:
    cow = tmp_path / "var/lib/homelab-ci/cow"
    cow.mkdir(parents=True)
    for name in ("slot1-root.aaa", "slot13-root.bbb", "slot2-cache.ccc"):
        (cow / name).touch()
    run = Recorder(
        {
            "dmsetup ls": (0, "ci-slot1-root\t(253:1)\nci-slot13-root\t(253:2)\n"
                           "ci-slot1-cache\t(253:3)\nci-slotx-root\t(253:4)\nother\t(253:5)\n"),
            "losetup -nO": (0, f"/dev/loop4 {cow}/slot1-root.aaa (deleted)\n"
                            f"/dev/loop5 {cow}/slot13-root.bbb (deleted)\n"
                            "/dev/loop6 /var/lib/homelab-ci/images/rootfs.ext4\n"),
        },
        {},
    )  # fmt: skip
    return Host(run=run, root=tmp_path, sleep=lambda _: None), run, cow


def test_detach_disks_takes_only_its_own_slot(tmp_path: Path) -> None:
    host, run, cow = detach_box(tmp_path)

    assert vm_disks.detach_disks(host, 1)

    assert run.ran("dmsetup remove") == [
        ["dmsetup", "remove", "--retry", "ci-slot1-root"],
        ["dmsetup", "remove", "--retry", "ci-slot1-cache"],
    ]
    assert run.ran("losetup -d") == [["losetup", "-d", "/dev/loop4"]]
    assert sorted(p.name for p in cow.iterdir()) == ["slot13-root.bbb", "slot2-cache.ccc"]


def test_detach_disks_without_a_slot_takes_every_vms(tmp_path: Path) -> None:
    host, run, cow = detach_box(tmp_path)
    run.answers["dmsetup remove --retry ci-slot13-root"] = (1, "")

    assert not vm_disks.detach_disks(host, None)

    assert [c[-1] for c in run.ran("dmsetup remove")] == [
        "ci-slot1-root",
        "ci-slot13-root",
        "ci-slot1-cache",
    ]
    assert run.ran("losetup -d") == [["losetup", "-d", "/dev/loop4", "/dev/loop5"]]
    assert list(cow.iterdir()) == []


def test_stop_vms_kills_every_vm_and_clears_its_files(tmp_path: Path) -> None:
    host, run, _ = detach_box(tmp_path)
    cgroup = tmp_path / "sys/fs/cgroup/firecracker/ci-slot2"
    cgroup.mkdir(parents=True)
    run_dir = tmp_path / "run/homelab-ci"
    (run_dir / "vms").mkdir(parents=True)
    (run_dir / "metadata-slot2.json").write_text("{}")
    (run_dir / "vms" / "homelab-ci-vm-x.json").write_text("{}")
    (run_dir / "slot2.lock").touch()
    jail = tmp_path / "srv/jailer/firecracker/ci-slot2/root"
    jail.mkdir(parents=True)

    stop_vms.stop_vms(host)

    assert run.calls[0] == ["systemctl", "stop", "homelab-ci-vm-*"]
    assert (cgroup / "cgroup.kill").read_text() == "1\n"
    assert len(run.ran("dmsetup remove")) == 3
    assert sorted(p.name for p in run_dir.rglob("*")) == ["slot2.lock", "vms"]
    assert not jail.parent.exists()
