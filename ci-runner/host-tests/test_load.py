#!/usr/local/bin/homelab-python3 -Es
"""Check that a VM in every slot at full tilt leaves the services responsive:
  sudo ci-runner/host-tests/test_load.py [SECONDS]
Boots a VM in every slot at the size the supervisor gives a job when others run
(2 vCPUs, 2 GiB), each burning all its CPUs and holding nearly all its memory,
while timing Home Assistant and Grafana once a second against a quiet baseline.
Ends with one PASS or FAIL line, which load_testing.py keeps (drain_and_test); exits 1
when a VM does not boot or finish its burn, a request fails, or a service's worst
response is over MAX_RESPONSE seconds.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from host_commands import run_command, start_process

DEFAULT_SLOTS = 5
QUIET_SECONDS = 15
# vm_size in vms.py for a job started while others run.
VM_SIZE = {"VCPUS": "2", "MEM_MIB": "2048"}
RUN_VM = "/usr/local/lib/homelab-ci/run_vm.py"
SERVICES = (("Home Assistant", ":8123/"), ("Grafana", ":5080/healthz"))


class Run(Protocol):
    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]: ...


class Waitable(Protocol):
    def wait(self) -> int: ...


@dataclass
class Host:
    run: Run = run_command
    start: Callable[[list[str], Path, Mapping[str, str]], Waitable] = start_process
    root: Path = Path("/")
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    is_root: Callable[[], bool] = lambda: os.geteuid() == 0


@dataclass(frozen=True)
class Timing:
    """One request: curl's time_total and http_code as it printed them, and the second
    since the phase started. Code 000 is no answer (a timeout, a refused connection)."""

    seconds: str
    code: str
    at: int

    @property
    def answered(self) -> bool:
        return 200 <= int(self.code) < 400

    def order(self) -> tuple[float, str]:
        """By time, ties by the whole line as text, as `sort -n` did: which second the
        worst is reported at stays the same."""
        return float(self.seconds), f"{self.seconds} {self.code} {self.at}"


@dataclass
class Samples:
    timings: list[list[Timing]]
    # MemAvailable, and data waiting to be written to disk (Dirty + Writeback), in KiB.
    memory: list[tuple[int, int]] = field(default_factory=list)


def slot_count(env: Mapping[str, str], root: Path) -> int:
    """The host's slot count (/etc/homelab-ci/slots, which install_host.py writes); an
    explicit SLOTS wins. 5 when neither is a number, at least 1, as in load_testing.py."""
    text = env.get("SLOTS", "")
    if not text:
        try:
            text = (root / "etc/homelab-ci/slots").read_text()
        except OSError:
            text = ""
    text = re.sub(r"\s", "", text)
    if re.fullmatch(r"[0-9]+", text, re.ASCII):
        return max(int(text), 1)
    return DEFAULT_SLOTS


def memory_sample(meminfo: str) -> tuple[int, int]:
    available = waiting = 0
    for line in meminfo.splitlines():
        name, _, rest = line.partition(":")
        if name == "MemAvailable":
            available = int(rest.split()[0])
        elif name in ("Dirty", "Writeback"):
            waiting += int(rest.split()[0])
    return available, waiting


def exit_status(code: int) -> int:
    """A wait result as the shell reports it: Popen gives -n for a process killed by signal n,
    the shell 128+n."""
    return 128 - code if code < 0 else code


def report(
    label: str, timings: Sequence[Sequence[Timing]], max_response: str
) -> tuple[list[str], list[str]]:
    """Each service's median and worst, and the reasons it breaks the bounds."""
    lines: list[str] = []
    reasons: list[str] = []
    for (name, _), service in zip(SERVICES, timings, strict=True):
        answered = sorted((t for t in service if t.answered), key=Timing.order)
        failures = [t for t in service if not t.answered]
        summary = "no answers"
        if answered:
            median, worst = answered[(len(answered) + 1) // 2 - 1], answered[-1]
            summary = (
                f"median {float(median.seconds):.3f}s, worst {float(worst.seconds):.3f}s"
                f" at {worst.at}s"
            )
        line = f"{label} {name}: {summary}, {len(failures)} failed"
        # Each failure as code@second, so a burst at boot shows apart from one under the burn.
        if failures:
            line += " (code@second:" + "".join(f" {t.code}@{t.at}s" for t in failures) + ")"
        lines.append(line)
        if failures:
            reasons.append(f"{label} {name}: {len(failures)} requests failed")
        if answered and float(answered[-1].seconds) > float(max_response):
            reasons.append(f"{label} {name}: worst {answered[-1].seconds}s over {max_response}s")
    return lines, reasons


def verdict(slots: int, reasons: Sequence[str], max_response: str) -> str:
    """The last line, which the supervisor keeps as the test's result."""
    if not reasons:
        return (
            f"PASS {slots} slots: every VM booted and finished, no request failed,"
            f" worst response within {max_response}s"
        )
    return f"FAIL {slots} slots: {'; '.join(reasons)}"


def burn_probe(seconds: int) -> str:
    """Hold nearly all the VM's free memory in a tmpfs for the whole run, so the host
    carries every slot's full size at once, while every CPU spins."""
    return f"""avail=$(awk '/^MemAvailable:/ {{print $2}}' /proc/meminfo)
burn_mib=$((avail / 1024 - 128))
mkdir -p /run/burn && mount -t tmpfs -o size=${{burn_mib}}M tmpfs /run/burn
head -c ${{burn_mib}}M /dev/zero > /run/burn/fill && echo "BURN_MEM ${{burn_mib}} MiB"
for i in $(seq $(nproc)); do timeout {seconds} sh -c 'while :; do :; done' & done
wait
[ "$(stat -c %s /run/burn/fill)" = $((burn_mib * 1048576)) ] && echo BURN_DONE"""


def vm_outcome(slot: int, status: int, log: str) -> tuple[str, str | None]:
    """What a slot's VM log shows, and the reason it fails the test, if any."""
    tail = "\n".join(log.splitlines()[-20:])
    if "=== probe start" not in log:
        return f"slot {slot}: did not boot; its log:\n{tail}", (
            f"slot {slot}: VM did not boot (exit {status})"
        )
    if "BURN_DONE" not in log:
        return f"slot {slot}: did not finish its burn; its log:\n{tail}", (
            f"slot {slot}: VM did not finish its burn"
        )
    held = "\n".join(re.findall(r"BURN_MEM ([0-9]*)", log))
    return f"slot {slot}: held {held} MiB, burned and exited", None


class LoadTest:
    def __init__(self, host: Host, urls: Sequence[str]) -> None:
        self.host = host
        self.urls = urls
        self.start = host.clock()

    def sample(self, count: int) -> Samples:
        """One timing per service per second for COUNT seconds."""
        samples = Samples([[] for _ in self.urls])
        for _ in range(count):
            for url, timings in zip(self.urls, samples.timings, strict=True):
                at = int(self.host.clock() - self.start)
                done = self.host.run(
                    [
                        *["curl", "-s", "-o", "/dev/null", "-m", "10", "-w"],
                        *["%{time_total} %{http_code}\n", url],
                    ]
                )
                # No curl (127): what curl itself prints for no answer.
                out = ["0", "000"] if done.returncode == 127 else done.stdout.split()
                if len(out) == 2:
                    timings.append(Timing(out[0], out[1], at))
            meminfo = (self.host.root / "proc/meminfo").read_text()
            samples.memory.append(memory_sample(meminfo))
            self.host.sleep(1)
        return samples


def main(argv: Sequence[str], host: Host | None = None) -> int:
    host = host or Host()
    if not host.is_root():
        print("run with sudo", file=sys.stderr)
        return 1
    seconds_text = argv[0] if argv else "60"
    if not re.fullmatch(r"[0-9]+", seconds_text, re.ASCII) or int(seconds_text) <= 15:
        print("SECONDS must be a number over 15", file=sys.stderr)
        return 2
    seconds = int(seconds_text)
    max_response = host.env.get("MAX_RESPONSE") or "1"
    # No verdict on a typo: the supervisor keeps that as not run; a FAIL would lower the slots.
    try:
        finite = math.isfinite(float(max_response))
    except ValueError:
        finite = False
    if not finite:
        print(f"MAX_RESPONSE must be a number of seconds, not {max_response!r}", file=sys.stderr)
        return 2
    slots = slot_count(host.env, host.root)
    run_vm = host.env.get("RUN_VM") or RUN_VM
    tailscale = host.run(["ip", "-4", "-br", "addr", "show", "tailscale0"])
    # No verdict without the address: the supervisor keeps that as not run and tries again,
    # where a FAIL would lower the slot count.
    if tailscale.returncode:
        return tailscale.returncode
    fields = tailscale.stdout.split()
    tailnet = fields[2].split("/")[0] if len(fields) > 2 else ""
    urls = [f"http://{tailnet}{path}" for _, path in SERVICES]

    test = LoadTest(host, urls)
    lines, reasons = report("quiet", test.sample(QUIET_SECONDS).timings, max_response)
    print("\n".join(lines))

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        metadata = tmp / "metadata.json"
        metadata.write_text(json.dumps({"task": "probe", "probe": burn_probe(seconds)}))
        # Seconds in the loaded report count from here, when the VMs start booting.
        test.start = host.clock()
        env = {**host.env, **VM_SIZE}
        vms = [
            host.start([run_vm, str(n), str(metadata), str(seconds + 120)], tmp / f"vm{n}.log", env)
            for n in range(slots)
        ]
        # Time the services from the start, so VM start-up counts too.
        loaded = test.sample(seconds - 5)
        for n, vm in enumerate(vms):
            status = exit_status(vm.wait())
            line, reason = vm_outcome(n, status, (tmp / f"vm{n}.log").read_text(errors="replace"))
            print(line)
            if reason:
                reasons.append(reason)

    lines, loaded_reasons = report("loaded", loaded.timings, max_response)
    print("\n".join(lines))
    reasons += loaded_reasons
    loadavg = " ".join((host.root / "proc/loadavg").read_text().split()[:3])
    print(f"load average: {loadavg}")
    lowest = min(available for available, _ in loaded.memory)
    print(f"host lowest MemAvailable: {lowest // 1024} MiB")
    most = max(waiting for _, waiting in loaded.memory)
    print(f"host most data waiting for the disk: {most // 1024} MiB")

    print(verdict(slots, reasons, max_response))
    return 1 if reasons else 0


if __name__ == "__main__":
    # Line by line, so the supervisor's journal shows progress and the verdict comes last.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main(sys.argv[1:]))
