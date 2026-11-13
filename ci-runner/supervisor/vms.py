"""The CI VMs: one started per queued job or kept ready as a standby, each a systemd unit of its
own, taken back after a supervisor restart, stopped once idle and cleared once ended. A VM's
console goes through ci_supervisor.py's `vm` mode (vm_main), which keeps its memory line."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
from collections.abc import Callable, Generator, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import cache_disks
import github_api
import job_waits
import telemetry
from github_api import GitHubApi, RateLimited, Want

if TYPE_CHECKING:
    from ci_supervisor import State

log = logging.getLogger("ci-supervisor").info


# The VM image (ci-runner/build-rootfs.sh); a standby booted before it was rebuilt is replaced.
ROOTFS = Path("/var/lib/homelab-ci/images/rootfs.ext4")


# The repo and cache label of the last build job, one per line, kept across restarts so
# the standby goes back to them. Older versions wrote the repo alone.
LAST_REPO_FILE = Path("/var/lib/homelab-ci/last-repo")


RUN_VM = Path("/usr/local/lib/homelab-ci/run_vm.py")
STATE_DIR = Path("/run/homelab-ci")
# Each running VM is a systemd unit of its own (homelab-ci-vm-<runner name>), so it outlives a
# restart of this supervisor; its record here, written before it starts, lets a restarted
# supervisor take it back (5 Nov: restarting killed every VM, and 20 jobs were cut off).
VMS_DIR = STATE_DIR / "vms"
UNIT_PREFIX = "homelab-ci-vm-"
CGROUP_ROOT = Path("/sys/fs/cgroup/firecracker")


# A VM covers a queued job of its repo and label only while it is starting: until its runner
# takes a job, and for at most this long: longer, a busy VM holds back the next job of its
# kind while slots are free.
BOOT_GRACE_SECONDS = 120
# Time for a VM to boot and connect. After it, a VM with no job while nothing is
# queued for its repo (the job was cancelled, or another runner took it) is stopped.
IDLE_AFTER_SECONDS = 30
VM_TIMEOUT_SECONDS = 3600
# Build VMs are kept booted and registered for the jobs likely next (see standby_wants), so
# a job starts in seconds instead of waiting for a VM. Each is replaced every 45 minutes
# while idle; its timeout leaves a job that starts just before then time to finish.
STANDBY_RECYCLE_SECONDS = 45 * 60


STANDBY_TIMEOUT_SECONDS = 2 * 3600
# A VM's CPU weight against the other VMs. A standby boots as a job takes the one before it, and
# its boot (about 6 CPU-seconds) took CPU from that job's checks; it boots at the least and gets
# a job VM's weight once booted or busy.
VM_CPU_WEIGHT = 20
STANDBY_CPU_WEIGHT = 1


CONSOLE_LINE_MAX = 4096
# What the job really needed (cache left out), the VM's memory and the kernel's out-of-memory
# kills, from the VM's ci-memwatch. A job can print it too: it only skews the counts.
MEM_LINE = re.compile(rb"HOMELAB-MEM 1 ([0-9]{1,7}) ([0-9]{1,7}) ([0-9]{1,9})")
# Counts of ended VMs for Prometheus (Alloy's textfile collector), and the share of its memory a
# job may need before its VM counts as near full.
MEMORY_METRICS = Path("/var/lib/homelab-metrics/ci-vm-memory.prom")
NEAR_FULL = 0.8
# A VM starts only if the host's available memory, less the VM's size, still leaves this
# much for the home's own services (the host's 16 GiB are shared with them). A booted VM
# touches far less than its size, so the check uses what is really free, not the sum of
# the sizes. /etc/homelab-ci/memory-reserve-mib changes it with no code change.
MEMINFO = Path("/proc/meminfo")
MEMORY_RESERVE_FILE = Path("/etc/homelab-ci/memory-reserve-mib")
DEFAULT_MEMORY_RESERVE_MIB = 3 * 1024
# No VM starts while tasks waited on the disk for more than DISK_WAIT_LIMIT percent of
# the last minute (`some avg60` in /proc/pressure/io), and over half that, one a minute,
# since a new VM's load takes about a minute to show; running VMs carry on. VM disks and
# swap share the host's one SSD with Grafana's database: on 5 Nov 9 VMs kept it at 70-95%,
# Grafana's checks failed on a locked database and posted false alerts. 50 is a judgement
# from hourly samples: quiet hours stayed under it, and Grafana already failed at some
# samples near it, hence the pacing below it.
IO_PRESSURE = Path("/proc/pressure/io")


@dataclass
class Launch:
    slot: int
    repo: str
    runner_id: int
    runner_name: str
    started: float
    metadata: Path
    # Booted ahead of any job; becomes an ordinary job VM once GitHub gives it one.
    standby: bool = False
    # A standby that just took a job still covers it until GitHub's job list catches up.
    covers_until: float = 0.0
    label: str = ""
    # GitHub showed its runner with a job: it no longer covers a queued one.
    busy: bool = False
    # GitHub showed its runner online and idle: a job it covers waits on GitHub, not the boot.
    ready: bool = False

    @property
    def unit(self) -> str:
        return f"{UNIT_PREFIX}{self.runner_name}"

    @property
    def record(self) -> Path:
        return VMS_DIR / f"{self.runner_name}.json"

    @property
    def memory_file(self) -> Path:
        """Its latest memory line, written by its console reader (`vm` mode)."""
        return VMS_DIR / f"{self.runner_name}.mem"

    @property
    def want(self) -> Want:
        return (self.repo, self.label)


def launch(
    gh: GitHubApi,
    state: State,
    repo: str,
    slot: int,
    now: float,
    *,
    cache: Path | None,
    label: str = "",
    standby: bool = False,
    jobs: Iterable[int] = (),
    load_test: Mapping[str, object],
) -> None:
    """Start a VM for repo and label. `jobs` are the queued job ids it may take, whose
    waits so far go into its metadata, with the host's last load test."""
    name = f"{github_api.RUNNER_PREFIX}{slot}-{int(now)}"
    # The cache label is registered too, so a job asking for it runs only on such a VM.
    labels = [*github_api.RUNNER_LABELS, label] if label else list(github_api.RUNNER_LABELS)
    registration = gh.request(
        "POST",
        f"/repos/{repo}/actions/runners/generate-jitconfig",
        {"name": name, "runner_group_id": 1, "labels": labels, "work_folder": "_work"},
    )
    runner_id = int(registration["runner"]["id"])
    metadata = STATE_DIR / f"metadata-slot{slot}.json"
    vcpus, mem_mib = next_size(state, standby)
    timeout = STANDBY_TIMEOUT_SECONDS if standby else VM_TIMEOUT_SECONDS
    run = Launch(slot, repo, runner_id, name, now, metadata, standby, label=label)
    # Unknown counts as public: the VM's guard then takes only Colin's jobs.
    known = state.serving.get(repo)
    public = not (known and known.served and not known.public)
    try:
        fd = os.open(metadata, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(
                {
                    "task": "runner",
                    "jitconfig": registration["encoded_jit_config"],
                    "waits": job_waits.vm_waits(state, jobs, now),
                    "load_test": dict(load_test),
                    "serves": {"repo": repo, "public": public},
                },
                f,
            )
        # The record first: a supervisor that dies right after starting the VM still finds it.
        write_status(run.record, record(run))
        weight = STANDBY_CPU_WEIGHT if standby else VM_CPU_WEIGHT
        env = {"VCPUS": str(vcpus), "MEM_MIB": str(mem_mib), "CACHE_IMAGE": str(cache or "")}
        env["CPU_WEIGHT"] = str(weight)
        (cache_disks.OUTBOX_DIR / f"{name}.json").write_text(json.dumps({"repo": repo}))
        env["CACHE_OUT"] = str(cache_disks.OUTBOX_DIR / f"{name}.tar")
        start_vm(run, timeout, env)
    except BaseException:
        # It may have started anyway (systemd-run timing out, say).
        subprocess.run(["systemctl", "stop", "--no-block", run.unit], check=False, timeout=30)
        forget(run)
        github_api.delete_runner(gh, repo, runner_id, name)
        raise
    state.launches[slot] = run
    # Only a job makes a repo polled every time, not a standby booted for it (4 Nov: one
    # booted after a restart kept a repo with no work polled every poll).
    if not standby:
        state.used.add(repo)
        note_last_job(state, repo, label)
    kind = "standby" if standby else "VM"
    disk = cache.name if cache else "no cache disk"
    log(f"slot {slot}: started {kind} {name} for {repo}, {disk} ({vcpus} vCPUs, {mem_mib} MiB)")


def note_last_job(state: State, repo: str, label: str) -> None:
    """The repo and cache label the build standby boots for next."""
    if (repo, label) != (state.last_repo, state.last_label):
        state.last_repo, state.last_label = repo, label
        remember_last_job(repo, label)


def remember_last_job(repo: str, label: str) -> None:
    remember(LAST_REPO_FILE, f"{repo}\n{label}\n")


def remember(path: Path, text: str) -> None:
    """Kept for the next start; losing it only sends the first standby elsewhere."""
    try:
        path.write_text(text)
    except OSError as e:
        log(f"could not keep the last job's repo: {type(e).__name__}: {e}")


def recalled_last_job() -> Want:
    """The last build job's repo and cache label ("" for none)."""
    try:
        lines = LAST_REPO_FILE.read_text().split()
    except OSError:
        return ("", "")
    return (lines[0] if lines else "", lines[1] if len(lines) > 1 else "")


def record(run: Launch) -> dict[str, Any]:
    """What a restarted supervisor needs to take a running VM back: its launch facts."""
    return {
        "slot": run.slot,
        "repo": run.repo,
        "runner_id": run.runner_id,
        "runner_name": run.runner_name,
        "started": run.started,
        "metadata": str(run.metadata),
        "standby": run.standby,
        "label": run.label,
    }


def start_vm(run: Launch, timeout: int, env: Mapping[str, str]) -> None:
    """Start the VM as a systemd unit of its own, so a restart of this supervisor leaves it
    running. Its console goes through this script's `vm` mode, which keeps its memory line.
    The unit stops before the CI network does, at shutdown."""
    vm = [str(run.slot), str(run.metadata), str(timeout), "--memory", str(run.memory_file)]
    subprocess.run(
        [
            "systemd-run",
            f"--unit={run.unit}",
            "--collect",
            "--quiet",
            # Stops before the network does at shutdown; Wants, not Requires, as the network
            # unit says: restarting it must not take running VMs with it.
            "--property=After=homelab-ci-net.service",
            "--property=Wants=homelab-ci-net.service",
            "--property=SyslogIdentifier=homelab-ci-vm",
            *(f"--setenv={k}={v}" for k, v in env.items()),
            sys.executable,
            # As the units run it: -I would drop its folder, where telemetry.py is installed.
            "-Es",
            str(Path(__file__).resolve().with_name("ci_supervisor.py")),
            "vm",
            *vm,
        ],
        check=True,
        timeout=30,
    )


def running_vms() -> set[str] | None:
    """The VM units still running (stopping counts), or None if systemd could not be asked."""
    try:
        out = subprocess.run(
            [
                "systemctl",
                "list-units",
                "--plain",
                "--no-legend",
                "--state=active,activating,deactivating",
                f"{UNIT_PREFIX}*",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    except OSError, subprocess.SubprocessError:
        return None
    return {line.split()[0].removesuffix(".service") for line in out.splitlines() if line.strip()}


def forget(run: Launch) -> None:
    """A VM's files, once it has ended, its memory counted first."""
    count_memory(run)
    for path in (run.record, run.memory_file, run.metadata):
        path.unlink(missing_ok=True)


# Ended job VMs, near-full ones and out-of-memory kills, each from 0, so the first kill after
# a restart still shows as an increase. Labelled kind="build" so the series go on unbroken.
MEMORY_COUNTS: dict[str, list[int]] = {"build": [0, 0, 0]}


def count_memory(run: Launch) -> None:
    """Log and count what an ended job VM needed. A standby that never took a job, or a VM
    whose console said nothing, is left out."""
    try:
        seen = json.loads(run.memory_file.read_text())
        peak, total, kills = int(seen["peak"]), int(seen["total"]), int(seen["kills"])
    except OSError, ValueError, KeyError, TypeError:
        return
    if run.standby or total <= 0:
        return
    log(
        f"slot {run.slot}: {run.runner_name} (build) needed at least {peak} MiB of"
        f" {total}, {kills} out-of-memory kills"
    )
    counts = MEMORY_COUNTS["build"]
    counts[0] += 1
    counts[1] += peak > NEAR_FULL * total
    counts[2] += kills
    write_memory_metrics()


def write_memory_metrics() -> None:
    """Replace the textfile whole; a failed write only leaves the last counts."""
    names = (
        "homelab_ci_vms_ended_total",
        "homelab_ci_vms_near_full_total",
        "homelab_ci_vm_oom_kills_total",
    )
    lines = [
        f'{name}{{kind="{kind}"}} {counts[i]}'
        for i, name in enumerate(names)
        for kind, counts in MEMORY_COUNTS.items()
    ]
    try:
        telemetry.replace_file(MEMORY_METRICS, "\n".join(lines) + "\n")
    except OSError as e:
        log(f"could not write {MEMORY_METRICS}: {type(e).__name__}: {e}")


def adopt(state: State) -> None:
    """Take back the VMs a previous supervisor started and that still run; delete the records
    of those that ended, and stop a VM unit without a record (nothing could track it)."""
    running = running_vms()
    if running is None:
        raise RuntimeError("cannot list the running VMs")
    VMS_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    for path in VMS_DIR.glob("*.json"):
        try:
            fields = json.loads(path.read_text())
            run = Launch(
                int(fields["slot"]),
                str(fields["repo"]),
                int(fields["runner_id"]),
                str(fields["runner_name"]),
                float(fields["started"]),
                Path(fields["metadata"]),
                standby=bool(fields["standby"]),
                label=str(fields["label"]),
            )
        except OSError, ValueError, TypeError, KeyError:
            path.unlink(missing_ok=True)
            continue
        if run.unit in running and run.slot not in state.launches:
            state.launches[run.slot] = run
            log(f"slot {run.slot}: took back {run.runner_name} for {run.repo}")
        else:
            forget(run)
    known = {run.unit for run in state.launches.values()}
    for unit in running - known:
        log(f"{unit} has no record, stopping it")
        subprocess.run(["systemctl", "stop", "--no-block", unit], check=False, timeout=30)


def vm_main(argv: list[str]) -> int:
    """`vm SLOT METADATA TIMEOUT [--memory FILE]`: run_vm.py's console reader, the main process
    of the VM's unit. It reads the console to the end (a full pipe would stall the VM), keeps
    the latest memory line in FILE, and exits with run_vm.py's code: 128 + the signal when a stop
    ended it, as a shell reports it (a negative code would read as 241 and fail the unit)."""
    parser = argparse.ArgumentParser(prog="ci_supervisor.py vm")
    parser.add_argument("slot")
    parser.add_argument("metadata")
    parser.add_argument("timeout")
    parser.add_argument("--memory", type=Path)
    args = parser.parse_args(argv)
    # Stopping the unit signals every process in it: run_vm.py cleans up on its own, and this
    # reader keeps reading until it has (a closed pipe would cut its cleanup short).
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    process = subprocess.Popen(
        [str(RUN_VM), args.slot, args.metadata, args.timeout],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    memory = (lambda seen: write_status(args.memory, seen)) if args.memory else None
    read_console(process.stdout, memory)
    code = process.wait()
    return 128 - code if code < 0 else code


def memory_line(raw: bytes) -> dict[str, int] | None:
    """One console line as ci-memwatch's report, or None unless it is exactly that."""
    match = MEM_LINE.fullmatch(raw.removesuffix(b"\n").removesuffix(b"\r"))
    if not match:
        return None
    return {"peak": int(match[1]), "total": int(match[2]), "kills": int(match[3])}


def read_console(stream: IO[bytes], memory: Callable[[dict[str, int]], None] | None = None) -> None:
    """Read a VM's console until it ends, handing each memory line to `memory`. It must never
    stop early: once the pipe fills, the VM's console, and with it the job, would stall. A line
    over CONSOLE_LINE_MAX bytes is dropped, to its end, without being held in memory; the rest
    of the console is read and dropped, never logged."""
    with stream:
        too_long = False
        while chunk := stream.readline(CONSOLE_LINE_MAX):
            whole = chunk.endswith(b"\n")
            fits = not too_long and (whole or len(chunk) < CONSOLE_LINE_MAX)
            if memory and fits and (seen := memory_line(chunk)):
                memory(seen)
            too_long = not whole


def vm_size(running: int) -> tuple[int, int]:
    """A job alone gets the whole machine's cores; with others running, each gets half."""
    return (4, 4096) if running == 0 else (2, 2048)


def next_size(state: State, standby: bool) -> tuple[int, int]:
    """A standby outlives the jobs around it, so it is sized for running alone."""
    return vm_size(0 if standby else len(state.launches))


def mem_available_mib() -> int:
    """MemAvailable from /proc/meminfo, in MiB."""
    for line in MEMINFO.read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise ValueError(f"no MemAvailable in {MEMINFO}")


def disk_wait_percent() -> float:
    """How much of the last minute some task waited on the disk, in percent (0 if unknown)."""
    try:
        some = IO_PRESSURE.read_text().splitlines()[0].split()
        return float(dict(f.split("=") for f in some[1:])["avg60"])
    except OSError, IndexError, KeyError, ValueError:
        return 0.0


def memory_reserve_mib() -> int:
    try:
        return max(0, int(MEMORY_RESERVE_FILE.read_text().strip()))
    except OSError, ValueError:
        return DEFAULT_MEMORY_RESERVE_MIB


def may_be_idle(age: float, queued_for_repo: int) -> bool:
    """Worth asking GitHub whether the VM has a job: it has had time to connect,
    and no queued job in its repo could still be on its way to it."""
    return age >= IDLE_AFTER_SECONDS and queued_for_repo == 0


def is_idle(gh: GitHubApi, run: Launch) -> bool:
    """GitHub says the VM's runner is connected and has no job."""
    try:
        runner = gh.request("GET", f"/repos/{run.repo}/actions/runners/{run.runner_id}")
    except urllib.error.HTTPError as e:
        if e.code == 404:  # finished and removed itself; the VM is ending
            return False
        raise
    note_ready(run, runner)
    return runner.get("busy") is False


def has_job(gh: GitHubApi, run: Launch) -> bool:
    """GitHub says the VM's runner has taken a job (or has finished and gone)."""
    try:
        runner = gh.request("GET", f"/repos/{run.repo}/actions/runners/{run.runner_id}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return True
        raise
    note_ready(run, runner)
    return runner.get("busy") is True


def note_ready(run: Launch, runner: Mapping[str, Any]) -> None:
    if runner.get("status") == "online" and runner.get("busy") is False:
        run.ready = True


def on_its_way(gh: GitHubApi, run: Launch, now: float, queued_for_want: int) -> bool:
    """The VM may still take a queued job of its repo and label: a standby, one that just
    took a job, or one starting whose runner has no job yet. GitHub is asked only while a
    job it could cover is queued, at most once a poll."""
    if run.standby or now < run.covers_until:
        return True
    if run.busy or now - run.started >= BOOT_GRACE_SECONDS:
        return False
    if queued_for_want:
        run.busy = has_job(gh, run)
    return not run.busy


def stop(run: Launch, why: str) -> None:
    log(f"slot {run.slot}: {run.runner_name} {why}, stopping it")
    subprocess.run(["systemctl", "stop", "--no-block", run.unit], check=False, timeout=30)
    # Do not rely on run_vm.py's trap alone: the VM lives in its own cgroup.
    kill = CGROUP_ROOT / f"ci-slot{run.slot}" / "cgroup.kill"
    if kill.exists():
        kill.write_text("1")


def stop_if_still_idle(gh: GitHubApi, run: Launch, why: str) -> None:
    """Asked twice, so a job assigned between the two answers is not killed."""
    if is_idle(gh, run):
        time.sleep(2)
        if is_idle(gh, run):
            stop(run, why)


def stop_idle(gh: GitHubApi, run: Launch, now: float, queued_for_repo: int) -> None:
    """A VM whose job was cancelled or taken by another runner would wait out its
    timeout; stop it as soon as it is clearly not going to get one."""
    if may_be_idle(now - run.started, queued_for_repo):
        stop_if_still_idle(gh, run, "has no job")


def image_since(started: float) -> bool:
    """Whether the VM image was rebuilt after a VM booted: its next job would run the old one."""
    try:
        return ROOTFS.stat().st_mtime > started
    except OSError:
        return False


def full_weight(run: Launch) -> None:
    """A booted or busy standby gets a job VM's CPU weight (STANDBY_CPU_WEIGHT says why)."""
    weight = CGROUP_ROOT / f"ci-slot{run.slot}" / "cpu.weight"
    try:
        if weight.read_text().strip() != str(VM_CPU_WEIGHT):
            weight.write_text(f"{VM_CPU_WEIGHT}\n")
    except FileNotFoundError:
        pass  # Gone with its VM.
    except OSError as e:
        log(f"slot {run.slot}: could not raise {run.runner_name}'s CPU weight: {e}")


def check_standby(gh: GitHubApi, run: Launch, now: float, served: bool) -> None:
    """A standby that got a job becomes an ordinary VM; an old idle one is replaced."""
    if run.ready:
        full_weight(run)
    if not served:
        stop(run, "serves a repo that is no longer served")
    elif not is_idle(gh, run):
        full_weight(run)  # booting or busy, it boots no further at the least weight
        if now - run.started >= IDLE_AFTER_SECONDS:  # connected, so not idle means busy
            run.standby = False
            run.covers_until = now + 10
            log(f"slot {run.slot}: standby {run.runner_name} took a job")
    elif now - run.started >= STANDBY_RECYCLE_SECONDS:
        stop_if_still_idle(gh, run, "is an old standby")
    elif image_since(run.started):
        stop_if_still_idle(gh, run, "booted before the image was rebuilt")


def reap(
    gh: GitHubApi,
    state: State,
    now: float,
    queued: Mapping[Want, int] | None = None,
    served: Iterable[str] = (),
) -> None:
    """Clear finished VMs. With this poll's queue, also stop VMs left without a job."""
    served = set(served)
    running = running_vms()
    if running is None:
        return  # systemd did not answer: tried again next poll
    for slot, run in list(state.launches.items()):
        if run.unit in running:
            if queued is None:
                continue
            if run.standby:
                check_standby(gh, run, now, run.repo in served)
                if not run.standby:  # it took a job
                    state.used.add(run.repo)
                    # A restart must not take it for a standby.
                    with bookkeeping(run, "its record"):
                        write_status(run.record, record(run))
            else:
                stop_idle(gh, run, now, queued.get(run.want, 0))
            continue
        # One ended VM's bookkeeping failing is logged and its slot still freed: raised into
        # the poll, it would stop every job start until fixed.
        with bookkeeping(run, "bookkeeping"):
            forget(run)
        with bookkeeping(run, "bookkeeping"):
            github_api.delete_runner(gh, run.repo, run.runner_id, run.runner_name)
        # A run's next job often asks for the same repo and cache label seconds after this one
        # ends: the standby booting in the freed slot is for those, not for the last job
        # started, so the next job need not wait for a boot.
        if not run.standby:
            with bookkeeping(run, "bookkeeping"):
                note_last_job(state, run.repo, run.label)
        log(f"slot {slot}: {run.runner_name} finished")
        del state.launches[slot]


@contextlib.contextmanager
def bookkeeping(run: Launch, what: str) -> Generator[None]:
    """Logs a failure of one VM's after-the-fact bookkeeping instead of failing the poll. A
    rate limit still ends the poll, leaving the VM to be tried again on the next."""
    try:
        yield
    except RateLimited:
        raise
    except Exception as e:
        log(f"slot {run.slot}: {run.runner_name} {what} failed: {type(e).__name__}: {e}")


def write_status(path: Path, data: Mapping[str, Any]) -> None:
    replace_file(path, json.dumps(data))


def replace_file(path: Path, text: str) -> None:
    """Replace the file whole, in a folder anyone can read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o755)
    telemetry.replace_file(path, text)
