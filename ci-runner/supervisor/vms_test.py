import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import cache_disks
import ci_supervisor as sup
import github_api
import job_waits
import pytest
import vms
from cache_disks_test import fixture_outbox_dir  # noqa: F401 - autouse: a launch writes its record

from conftest import NoGitHub, linux_only

# The real readers, kept before plenty_of_memory replaces them.
READ_MEMORY, READ_RESERVE = vms.mem_available_mib, vms.memory_reserve_mib
READ_DISK_WAIT = vms.disk_wait_percent


@pytest.fixture(autouse=True, name="plenty_of_memory")
def fixture_plenty_of_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests do not depend on the machine running them: memory is never short unless a
    test says so."""
    monkeypatch.setattr(vms, "mem_available_mib", lambda: 1 << 20)
    monkeypatch.setattr(vms, "memory_reserve_mib", lambda: vms.DEFAULT_MEMORY_RESERVE_MIB)
    monkeypatch.setattr(vms, "disk_wait_percent", lambda: 0.0)


@pytest.mark.parametrize(
    ("age", "queued", "check"),
    [
        (5, 0, False),  # still booting
        (30, 0, True),  # booted, and no job left for it: ask GitHub
        (300, 1, False),  # a job is queued and may still be assigned to it
    ],
)
def test_asks_about_an_idle_vm_only_once_booted_with_nothing_queued(
    age: float, queued: int, check: bool
) -> None:
    assert vms.may_be_idle(age, queued) is check


@pytest.mark.parametrize(("running", "size"), [(0, (4, 4096)), (1, (2, 2048))])
def test_a_job_alone_gets_all_four_cores(running: int, size: tuple[int, int]) -> None:
    assert vms.vm_size(running) == size


def test_the_status_file_is_replaced_whole_and_readable_by_all(tmp_path: Path) -> None:
    path = tmp_path / "status" / "machines.json"
    vms.write_status(path, {"in_use": 1})
    vms.write_status(path, {"in_use": 2})

    assert json.loads(path.read_text()) == {"in_use": 2}
    linux_only("POSIX file modes")
    assert path.stat().st_mode & 0o777 == 0o644
    assert path.parent.stat().st_mode & 0o777 == 0o755
    assert [p.name for p in path.parent.iterdir()] == ["machines.json"]  # no temp file left


def test_a_failed_status_write_leaves_the_old_file_and_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "machines.json"
    vms.write_status(path, {"in_use": 1})

    with pytest.raises(TypeError):
        vms.write_status(path, {"in_use": object()})

    assert json.loads(path.read_text()) == {"in_use": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["machines.json"]


def test_the_console_reader_drops_over_long_lines_and_reads_to_the_end() -> None:
    first = b"HOMELAB-MEM 1 812 1985 0\n"
    over_long = b"HOMELAB-MEM 1 990 1985 0" + b" " * 5000 + b"\n"
    console = io.BytesIO(b"Linux version 6.1\n" + first + over_long + b"login:")
    kept: list[dict[str, Any]] = []
    vms.read_console(console, kept.append)
    assert [line["peak"] for line in kept] == [812]
    assert console.closed


def test_every_vm_reports_its_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[list[str]] = []
    monkeypatch.setattr(vms.subprocess, "run", lambda cmd, **k: started.append(cmd))
    run = vms.Launch(1, "ColinCee/servo", 2, "fc-1", 0.0, Path("m1"))
    START_VM(run, 60, {})
    assert started[0][-2:] == ["--memory", str(run.memory_file)]


def test_a_vm_unit_finds_the_modules_installed_beside_the_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[list[str]] = []
    monkeypatch.setattr(vms.subprocess, "run", lambda cmd, **k: started.append(cmd))
    START_VM(vms.Launch(1, "ColinCee/servo", 2, "fc-1", 0.0, Path("m1")), 60, {})
    monkeypatch.undo()
    command = started[0]
    at = command.index(sys.executable)
    script = command.index(str(Path(sup.__file__).resolve()))
    (tmp_path / "telemetry.py").write_text("", encoding="utf-8")
    (tmp_path / "probe.py").write_text("import telemetry\n", encoding="utf-8")
    probe = [*command[at:script], str(tmp_path / "probe.py")]
    done = subprocess.run(probe, cwd="/", capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_reads_available_memory_and_the_reserve_from_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16000000 kB\nMemAvailable:    8192000 kB\n")
    monkeypatch.setattr(vms, "MEMINFO", meminfo)
    reserve = tmp_path / "memory-reserve-mib"
    monkeypatch.setattr(vms, "MEMORY_RESERVE_FILE", reserve)
    assert READ_MEMORY() == 8000
    assert READ_RESERVE() == 3072  # no file: the default
    reserve.write_text("2048\n")
    assert READ_RESERVE() == 2048
    reserve.write_text("lots\n")
    assert READ_RESERVE() == 3072


def test_an_unreadable_pressure_file_does_not_hold_vms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vms, "IO_PRESSURE", tmp_path / "missing")
    assert READ_DISK_WAIT() == 0.0


def test_a_standby_does_not_make_its_repo_polled_every_time_a_job_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Registers(NoGitHub):
        def request(self, method: str, path: str, body: object = None) -> dict[str, Any]:
            return {"runner": {"id": 1}, "encoded_jit_config": "x"}

    monkeypatch.setattr(vms, "STATE_DIR", tmp_path)
    monkeypatch.setattr(vms, "LAST_REPO_FILE", tmp_path / "last-repo")
    state = sup.State()
    vms.launch(Registers(), state, "o/spare", 0, 0.0, cache=None, standby=True, load_test={})
    assert state.used == set()
    # Once the standby takes a job, its repo counts.
    monkeypatch.setattr(vms, "is_idle", lambda gh, run: False)
    vms.reap(NoGitHub(), state, 60.0, {}, ["o/spare"])
    assert state.used == {"o/spare"}
    vms.launch(Registers(), state, "o/job", 1, 0.0, cache=None, load_test={})
    assert state.used == {"o/spare", "o/job"}


# The real one, before the fixture below replaces it in every test.
START_VM = vms.start_vm


class EveryUnit(set[Any]):
    """Every VM unit counts as running, unless a test says otherwise."""

    def __contains__(self, item: object) -> bool:
        return True


@pytest.fixture(autouse=True, name="vm_records")
def fixture_vm_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """VM records in a temporary folder, VMs never really started, and all still running."""
    monkeypatch.setattr(vms, "VMS_DIR", tmp_path / "vms")
    monkeypatch.setattr(vms, "start_vm", lambda run, timeout, env: None)
    monkeypatch.setattr(vms, "running_vms", lambda: EveryUnit())
    return tmp_path / "vms"


def test_a_restarted_supervisor_takes_back_running_vms_and_drops_ended_ones(
    vm_records: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = vms.Launch(3, "o/ok", 7, "fc-3-100", 100.0, Path("m3"), label="fc-cache-o--ok")
    ended = vms.Launch(4, "o/ok", 8, "fc-4-100", 100.0, Path("m4"))
    for run in (running, ended):
        vms.write_status(run.record, vms.record(run))
    stopped: list[list[str]] = []
    monkeypatch.setattr(vms.subprocess, "run", lambda cmd, **k: stopped.append(cmd))
    units = {running.unit, "homelab-ci-vm-fc-9-1"}  # the second has no record
    monkeypatch.setattr(vms, "running_vms", lambda: units)

    state = sup.State()
    vms.adopt(state)

    assert state.launches == {3: running}
    assert not ended.record.exists()
    assert stopped == [["systemctl", "stop", "--no-block", "homelab-ci-vm-fc-9-1"]]


def test_a_vm_that_ended_is_forgotten_and_its_runner_removed(
    vm_records: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = vms.Launch(0, "o/ok", 7, "fc-0-1", 0.0, vm_records / "metadata")
    vms.write_status(run.record, vms.record(run))
    removed: list[str] = []
    monkeypatch.setattr(
        github_api, "delete_runner", lambda gh, repo, rid, name: removed.append(name)
    )
    monkeypatch.setattr(vms, "running_vms", lambda: set())
    state = sup.State(launches={0: run})

    vms.reap(NoGitHub(), state, 10.0)

    assert state.launches == {} and removed == ["fc-0-1"] and not run.record.exists()


def test_reaping_waits_while_systemd_cannot_be_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vms, "running_vms", lambda: None)
    run = vms.Launch(0, "o/ok", 7, "fc-0-1", 0.0, Path("m"))
    state = sup.State(launches={0: run})
    vms.reap(NoGitHub(), state, 10.0)
    assert state.launches == {0: run}


def test_a_standby_that_took_a_job_is_recorded_as_a_job_vm(
    vm_records: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = vms.Launch(0, "o/ok", 7, "fc-0-1", 0.0, Path("m"), True)
    vms.write_status(run.record, vms.record(run))
    monkeypatch.setattr(vms, "is_idle", lambda gh, r: False)
    state = sup.State(launches={0: run})
    vms.reap(NoGitHub(), state, 60.0, {}, ["o/ok"])
    assert json.loads(run.record.read_text())["standby"] is False
    assert "o/ok" in state.used  # a job keeps its repo polled


def test_the_console_reader_keeps_only_exact_memory_lines() -> None:
    seen: list[dict[str, int]] = []
    console = io.BytesIO(
        b"\nHOMELAB-MEM 1 812 1985 0\n"
        b"noise HOMELAB-MEM 1 9999 1985 0\n"
        b"HOMELAB-MEM 1 -5 1985 0\n"
        b"\nHOMELAB-MEM 1 990 1985 2\r\n"
    )
    vms.read_console(console, seen.append)
    assert seen == [
        {"peak": 812, "total": 1985, "kills": 0},
        {"peak": 990, "total": 1985, "kills": 2},
    ]


@pytest.fixture
def memory(supervisor_log: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """Fresh counts, a temporary textfile, and the log lines."""
    # The supervisor's own kinds, so a kind it lacks fails here as it would on the host.
    monkeypatch.setattr(vms, "MEMORY_COUNTS", {k: [0, 0, 0] for k in vms.MEMORY_COUNTS})
    monkeypatch.setattr(vms, "MEMORY_METRICS", tmp_path / "ci-vm-memory.prom")
    return supervisor_log


def _ended(
    vm_records: Path, label: str, peak: int, kills: int, standby: bool = False
) -> vms.Launch:
    run = vms.Launch(6, "o/ok", 7, "fc-6-1", 0.0, vm_records / "m", standby, label=label)
    vms.write_status(run.memory_file, {"peak": peak, "total": 1000, "kills": kills})
    return run


def test_an_ended_vm_is_counted_by_kind_and_its_memory_file_removed(
    vm_records: Path, memory: list[str]
) -> None:
    vms.write_memory_metrics()
    assert 'homelab_ci_vm_oom_kills_total{kind="build"} 0' in vms.MEMORY_METRICS.read_text()
    run = _ended(vm_records, "", peak=850, kills=1)
    vms.forget(run)
    text = vms.MEMORY_METRICS.read_text()
    assert 'homelab_ci_vms_ended_total{kind="build"} 1' in text
    assert 'homelab_ci_vms_near_full_total{kind="build"} 1' in text  # 850 of 1000 is over 80%
    assert 'homelab_ci_vm_oom_kills_total{kind="build"} 1' in text
    assert memory == [
        "slot 6: fc-6-1 (build) needed at least 850 MiB of 1000, 1 out-of-memory kills"
    ]
    assert not run.memory_file.exists()


def test_an_ended_vm_with_a_cache_label_is_counted(vm_records: Path, memory: list[str]) -> None:
    run = _ended(vm_records, "fc-cache-o--build", peak=300, kills=0)
    vms.forget(run)
    assert vms.MEMORY_COUNTS["build"][0] == 1


def test_a_standby_that_never_took_a_job_or_a_silent_vm_is_not_counted(
    vm_records: Path, memory: list[str]
) -> None:
    vms.forget(_ended(vm_records, "", peak=300, kills=0, standby=True))
    vms.forget(vms.Launch(5, "o/ok", 7, "fc-5-1", 0.0, vm_records / "m"))  # no memory line came
    assert memory == [] and vms.MEMORY_COUNTS["build"] == [0, 0, 0]


def test_a_vm_is_told_how_long_its_queued_jobs_waited_and_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Registers(NoGitHub):
        def request(self, method: str, path: str, body: object = None) -> dict[str, Any]:
            return {"runner": {"id": 1}, "encoded_jit_config": "x"}

    monkeypatch.setattr(vms, "STATE_DIR", tmp_path)
    monkeypatch.setattr(cache_disks, "OUTBOX_DIR", tmp_path)
    monkeypatch.setattr(vms, "LAST_REPO_FILE", tmp_path / "last-repo")
    monkeypatch.setattr(vms, "start_vm", lambda run, timeout, env: None)
    state = sup.State()
    job_waits.note_waits(
        state,
        ["o/ok"],
        {7: job_waits.Reason("o/ok", "all 2 build slots in use", job_waits.WaitKind.SLOTS)},
        100.0,
    )
    vms.launch(Registers(), state, "o/ok", 0, 340.0, cache=None, jobs=[9, 7], load_test={})
    metadata = json.loads((tmp_path / "metadata-slot0.json").read_text())
    assert metadata["waits"] == {
        "7": {"seconds": 240, "why": "all 2 build slots in use"},
        "9": {"seconds": 0, "why": ""},
    }
    monkeypatch.setattr(job_waits, "VM_WAITS_MAX", 1)
    vms.launch(Registers(), state, "o/ok", 1, 340.0, cache=None, jobs=[9, 7], load_test={})
    assert list(json.loads((tmp_path / "metadata-slot1.json").read_text())["waits"]) == ["7"]


def test_a_vm_is_told_its_repo_and_counts_it_public_unless_known_served_and_private(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Registers(NoGitHub):
        def request(self, method: str, path: str, body: object = None) -> dict[str, Any]:
            return {"runner": {"id": 1}, "encoded_jit_config": "x"}

    monkeypatch.setattr(vms, "STATE_DIR", tmp_path)
    monkeypatch.setattr(cache_disks, "OUTBOX_DIR", tmp_path)
    monkeypatch.setattr(vms, "LAST_REPO_FILE", tmp_path / "last-repo")
    monkeypatch.setattr(vms, "start_vm", lambda run, timeout, env: None)
    state = sup.State(serving={"o/private": github_api.Serving(True, 0.0, "main")})
    for slot, repo, public in [(0, "o/private", False), (1, "o/unknown", True)]:
        vms.launch(Registers(), state, repo, slot, 0.0, cache=None, load_test={})
        metadata = json.loads((tmp_path / f"metadata-slot{slot}.json").read_text())
        assert metadata["serves"] == {"repo": repo, "public": public}


def test_an_idle_standby_booted_before_the_image_was_rebuilt_is_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = tmp_path / "rootfs.ext4"
    image.write_text("")
    os.utime(image, (100.0, 100.0))
    monkeypatch.setattr(vms, "ROOTFS", image)
    monkeypatch.setattr(vms, "is_idle", lambda gh, run: True)
    monkeypatch.setattr(vms.time, "sleep", lambda s: None)
    stopped: list[str] = []
    monkeypatch.setattr(vms, "stop", lambda run, why: stopped.append(why))
    older = vms.Launch(0, "o/r", 1, "fc-0", 50.0, Path("m"), True)
    newer = vms.Launch(1, "o/r", 2, "fc-1", 150.0, Path("m"), True)
    for run in (older, newer):
        vms.check_standby(NoGitHub(), run, 200.0, True)
    assert stopped == ["booted before the image was rebuilt"]


def test_a_vm_stopped_by_a_signal_exits_as_a_shell_reports_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    linux_only("a script run by its #! line kills itself with SIGTERM")
    # run_vm.py re-raises the stop signal; a negative code from sys.exit would read as 241.
    script = tmp_path / "run_vm.py"
    script.write_text("#!/bin/sh\nkill -TERM $$\n")
    script.chmod(0o755)
    monkeypatch.setattr(vms, "RUN_VM", script)
    monkeypatch.setattr(vms.signal, "signal", lambda *a: None)
    assert vms.vm_main(["0", "metadata.json", "60"]) == 143


def test_a_standby_boots_at_the_least_cpu_weight_and_gets_a_job_s_once_ready_or_busy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Registers(NoGitHub):
        def request(self, method: str, path: str, body: object = None) -> dict[str, Any]:
            return {"runner": {"id": 1}, "encoded_jit_config": "x"}

    weights: list[str] = []
    monkeypatch.setattr(
        vms, "start_vm", lambda run, timeout, env: weights.append(env["CPU_WEIGHT"])
    )
    monkeypatch.setattr(vms, "STATE_DIR", tmp_path)
    monkeypatch.setattr(vms, "LAST_REPO_FILE", tmp_path / "last-repo")
    monkeypatch.setattr(vms, "CGROUP_ROOT", tmp_path)
    state = sup.State()
    vms.launch(Registers(), state, "o/r", 0, 0.0, cache=None, standby=True, load_test={})
    vms.launch(Registers(), state, "o/r", 1, 0.0, cache=None, load_test={})
    assert weights == [str(vms.STANDBY_CPU_WEIGHT), str(vms.VM_CPU_WEIGHT)]

    for slot in (0, 1):
        (tmp_path / f"ci-slot{slot}").mkdir()
        (tmp_path / f"ci-slot{slot}" / "cpu.weight").write_text("1\n")
    booting = vms.Launch(0, "o/r", 1, "fc-0", 0.0, Path("m"), True)
    monkeypatch.setattr(vms, "is_idle", lambda gh, run: True)
    vms.check_standby(NoGitHub(), booting, 10.0, True)
    assert (tmp_path / "ci-slot0" / "cpu.weight").read_text() == "1\n"
    booting.ready = True
    vms.check_standby(NoGitHub(), booting, 20.0, True)
    assert (tmp_path / "ci-slot0" / "cpu.weight").read_text() == f"{vms.VM_CPU_WEIGHT}\n"
    busy = vms.Launch(1, "o/r", 2, "fc-1", 0.0, Path("m"), True)
    monkeypatch.setattr(vms, "is_idle", lambda gh, run: False)
    vms.check_standby(NoGitHub(), busy, 60.0, True)
    assert (tmp_path / "ci-slot1" / "cpu.weight").read_text() == f"{vms.VM_CPU_WEIGHT}\n"
