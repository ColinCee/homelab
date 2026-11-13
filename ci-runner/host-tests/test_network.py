#!/usr/bin/env python3
"""Prove the microVM network rules from inside a real VM:
  sudo ci-runner/host-tests/test-network.sh
Boots a VM in slot 0 running network-probe.sh, and while it listens tries to connect to
it from a Docker container. Exits non-zero on any failure.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from host_commands import run_command, start_process

RUN_VM = "/usr/local/lib/homelab-ci/run-vm.sh"
BUSYBOX = "busybox:1.37"
LISTEN_SECONDS = 120


class Run(Protocol):
    def __call__(
        self, command: list[str], *, check: bool = False, quiet: bool = False
    ) -> subprocess.CompletedProcess[str]: ...


class Waitable(Protocol):
    def wait(self) -> int: ...


@dataclass
class Host:
    run: Run = run_command
    start: Callable[[list[str], Path, Mapping[str, str]], Waitable] = start_process
    sleep: Callable[[float], None] = time.sleep
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    is_root: Callable[[], bool] = lambda: os.geteuid() == 0


def inbound_drops(chain: str) -> int:
    """The packets counted by the forward chain's rule that matches on the VMs' interface
    (oifname): homelab-ci.nft drops inbound traffic to the VMs there."""
    last = None
    for item in json.loads(chain).get("nftables", []):
        expr = item.get("rule", {}).get("expr") if isinstance(item, dict) else None
        if expr and "oifname" in json.dumps(expr, separators=(",", ":")):
            for part in expr:
                packets = part.get("counter", {}).get("packets") if isinstance(part, dict) else None
                if packets is not None and packets is not False:
                    last = packets
    return int(last or 0)


def verdict(console: str, inbound: str) -> tuple[list[str], str | None]:
    """The probe's result lines, and the failure line if any check failed, none were
    counted, or inbound was not blocked."""
    results = [
        m.group(0) for line in console.split("\n") if (m := re.search(r"(PASS|FAIL) .*", line))
    ]
    if "INBOUND_CONNECTED" in console:
        inbound = "reached"
    passes = sum(r.startswith("PASS") for r in results)
    fails = sum(r.startswith("FAIL") for r in results)
    total = re.search(r"TOTAL ([0-9]+)", console)
    if total is None or passes != int(total.group(1)) or fails or inbound != "blocked":
        counted = total.group(1) if total else "?"
        return results, (
            f"NETWORK TEST FAILED ({passes} of {counted} passed, {fails} failed, inbound {inbound})"
        )
    return results, None


def main(host: Host | None = None, here: Path | None = None) -> int:
    host = host or Host()
    here = here or Path(__file__).resolve().parent
    if not host.is_root():
        print("run with sudo", file=sys.stderr)
        return 1
    run_vm = host.env.get("RUN_VM") or RUN_VM
    route = host.run(["ip", "-4", "route", "get", "1.1.1.1"], check=True).stdout
    lan = re.search(r" src ([0-9.]*)", route)
    host_lan = lan.group(1) if lan else ""
    tailscale = host.run(["ip", "-4", "-br", "addr", "show", "tailscale0"], check=True).stdout
    fields = tailscale.split()
    tailnet = fields[2].split("/")[0] if len(fields) > 2 else ""

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        probe = (here / "network-probe.sh").read_text()
        probe = probe.replace("@HOST_LAN@", host_lan).replace("@TAILNET@", tailnet)
        (tmp / "metadata.json").write_text(json.dumps({"task": "probe", "probe": probe}))
        console = tmp / "console.log"
        vm = host.start([run_vm, "0", str(tmp / "metadata.json"), "180"], console, host.env)

        # Inbound from a container (a forwarded path, like any other host on the network).
        # The drop counter must rise, so we know our rule stopped it, not a missing listener.
        def drops() -> int:
            chain = ["nft", "-j", "list", "chain", "inet", "homelab_ci", "forward"]
            return inbound_drops(host.run(chain, check=True).stdout)

        host.run(["docker", "pull", "-q", BUSYBOX], check=True)
        for _ in range(LISTEN_SECONDS):
            if "LISTENING" in console.read_text(errors="replace"):
                break
            host.sleep(1)
        before = drops()
        inbound = "blocked"
        nc = ["docker", "run", "--rm", "--network", "bridge", BUSYBOX, "nc", "-w", "3"]
        if host.run([*nc, "10.200.0.2", "8080"], quiet=True).returncode == 0:
            inbound = "reached"
        if drops() <= before:
            inbound = "not dropped by our rule"
        # And from Beelink's own network namespace.
        tcp = ["timeout", "4", "bash", "-c", "exec 3<>/dev/tcp/10.200.0.2/8080"]
        if host.run(tcp, quiet=True).returncode == 0:
            inbound = "reached from host"
        vm.wait()

        log = console.read_text(errors="replace")
        results, failed = verdict(log, inbound)
        print("\n".join(results))
        print(f"inbound from container: {inbound}")
        if failed:
            print(failed)
            print("\n".join(log.splitlines()[-30:]))
            return 1
    print(f"network test passed ({len(results)} checks, inbound blocked)")
    return 0


def entry() -> int:
    try:
        return main()
    except subprocess.CalledProcessError as e:
        return e.returncode


if __name__ == "__main__":
    sys.exit(entry())
