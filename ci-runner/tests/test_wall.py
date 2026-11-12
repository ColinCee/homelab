#!/usr/local/bin/homelab-python3 -Es
"""Prove the container wall (homelab-ci.nft, table homelab_wall):
  sudo ci-runner/tests/test-wall.sh
A throwaway container on its own Docker network must be stopped by the wall on the ports
Docker publishes on the Tailscale address, the router, the tailnet and the public address,
yet reach the internet and its network peer, and a container on the default bridge must
still resolve names. A stand-in LAN machine (a network namespace routing the Tailscale
address through Beelink) must be stopped on the published ports. "Stopped" means the
wall's drop counter rose, so a closed port or a broken probe cannot pass.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

IMAGE = "busybox:1.37"
NET = "homelab-wall-test"
NS = "homelab-wall-lan"
PEER = "homelab-wall-peer"
VETH = "homelab-wall0"
IPV4 = re.compile(r"([0-9]{1,3}\.){3}[0-9]{1,3}")


class Run(Protocol):
    def __call__(
        self, command: list[str], *, show: bool = False, quiet: bool = False
    ) -> subprocess.CompletedProcess[str]: ...


def run_command(
    command: list[str], *, show: bool = False, quiet: bool = False
) -> subprocess.CompletedProcess[str]:
    """SHOW sends stdout to the terminal, QUIET drops stderr. A missing program fails as
    it would in a shell, so the probe behind it fails rather than the whole test."""
    try:
        return subprocess.run(
            command,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            stdout=None if show else subprocess.PIPE,
            stderr=subprocess.DEVNULL if quiet else None,
            check=False,
        )
    except OSError as e:
        if not quiet:
            print(f"{command[0]}: {e}", file=sys.stderr)
        return subprocess.CompletedProcess(command, 127, "")


@dataclass
class Host:
    run: Run = run_command
    sleep: Callable[[float], None] = time.sleep
    is_root: Callable[[], bool] = lambda: os.geteuid() == 0


def counted(value: object) -> int:
    """Every counter's packets anywhere in nft's JSON, summed."""
    if isinstance(value, list):
        return sum(counted(item) for item in value)
    if not isinstance(value, dict):
        return 0
    total = sum(counted(item) for item in value.values())
    counter = value.get("counter")
    if isinstance(counter, dict):
        packets = counter.get("packets")
        if packets is not None and packets is not False:
            total += int(packets)
    return total


class Wall:
    def __init__(self, host: Host) -> None:
        self.host = host
        self.failures = 0

    def passed(self, line: str) -> None:
        print(f"PASS {line}")

    def failed(self, line: str) -> None:
        print(f"FAIL {line}")
        self.failures += 1

    def drops(self, chain: str) -> int | None:
        """None when nft cannot say, which no count can pass as a rise."""
        listing = self.host.run(["nft", "-j", "list", "chain", "inet", "homelab_wall", chain])
        if listing.returncode != 0:
            return None
        try:
            return counted(json.loads(listing.stdout))
        except ValueError, TypeError:
            return None

    def stopped(self, name: str, chain: str, probe: Callable[[], bool]) -> None:
        before = self.drops(chain)
        if probe():
            self.failed(f"{name}: reached")
            return
        after = self.drops(chain)
        if before is not None and after is not None and after > before:
            self.passed(f"{name}: dropped by the wall")
        else:
            self.failed(f"{name}: failed, but not by the wall")

    def reaches(self, name: str, probe: Callable[[], bool]) -> None:
        if probe():
            self.passed(f"{name}: reached")
        else:
            self.failed(f"{name}: not reached")

    def from_container(self, target: str) -> bool:
        address, _, port = target.rpartition(":")
        nc = ["docker", "run", "--rm", "--network", NET, IMAGE, "nc", "-w", "3", address, port]
        return self.host.run(nc, quiet=True).returncode == 0

    def from_lan(self, target: str) -> bool:
        address, _, port = target.rpartition(":")
        tcp = ["timeout", "3", "bash", "-c", f"exec 3<>/dev/tcp/{address}/{port}"]
        in_ns = ["ip", "netns", "exec", NS, *tcp]
        return self.host.run(in_ns, show=True, quiet=True).returncode == 0

    def shown(self, *command: str) -> bool:
        return self.host.run(list(command), show=True).returncode == 0


def cleanup(host: Host) -> None:
    for command in (
        ["docker", "rm", "-f", PEER],
        ["docker", "network", "rm", NET],
        ["ip", "netns", "del", NS],
        ["ip", "link", "del", VETH],
    ):
        host.run(command, quiet=True)


def field_of_first_line(text: str, index: int) -> str:
    lines = text.splitlines()
    fields = lines[0].split() if lines else []
    return fields[index] if len(fields) > index else ""


def published_on(ports: str, tailnet: str) -> list[str]:
    """The address:port pairs Docker publishes on the Tailscale address."""
    if not tailnet:
        return []
    found = re.findall(rf"{re.escape(tailnet)}:[0-9]+->[0-9]+/tcp", ports)
    return sorted({pair.split("-")[0] for pair in found})


def wall_test(host: Host) -> int:
    run = host.run
    tailscale = run(["ip", "-4", "-br", "addr", "show", "tailscale0"]).stdout
    tailnet = field_of_first_line(tailscale, 2).split("/")[0]
    router = field_of_first_line(run(["ip", "-4", "route", "show", "default"]).stdout, 2)
    wan_set = run(["nft", "list", "set", "inet", "homelab_wall", "wan4"], quiet=True).stdout
    wan = m.group(0) if (m := IPV4.search(wan_set)) else ""
    published = published_on(run(["docker", "ps", "--format", "{{.Ports}}"]).stdout, tailnet)

    if run(["nft", "list", "table", "inet", "homelab_wall"], quiet=True).returncode != 0:
        print("FAIL the homelab_wall table is not loaded")
        return 1
    if not published:
        print(f"FAIL nothing is published on {tailnet} to test against")
        return 1
    if not wan:
        print("FAIL the wall holds no public address")
        return 1
    run(["docker", "pull", "-q", IMAGE])
    run(["docker", "network", "create", NET])
    listen = ["nc", "-lk", "-p", "8080", "-e", "cat"]
    run(["docker", "run", "-d", "--name", PEER, "--network", NET, IMAGE, *listen])

    wall = Wall(host)
    for _ in range(10):
        if wall.from_container(f"{PEER}:8080"):
            break
        host.sleep(1)

    for target in [*published, f"{router}:80", "100.100.100.100:53", f"{wan}:443"]:
        wall.stopped(
            f"container to {target}", "from_container", lambda t=target: wall.from_container(t)
        )
    wall.reaches("container to the internet", lambda: wall.from_container("1.1.1.1:443"))
    wall.reaches("container to its network peer", lambda: wall.from_container(f"{PEER}:8080"))
    wall.reaches(
        "default-bridge container resolves names",
        lambda: wall.shown("docker", "run", "--rm", IMAGE, "nslookup", "example.com"),
    )

    # Beelink sees the veth as it sees enp1s0: not Docker's, a VM's or the tailnet's interface.
    for command in (
        ["ip", "netns", "add", NS],
        ["ip", "link", "add", VETH, "type", "veth", "peer", "name", "lan", "netns", NS],
        ["ip", "addr", "add", "10.254.254.1/30", "dev", VETH],
        ["ip", "link", "set", VETH, "up"],
        ["ip", "-n", NS, "addr", "add", "10.254.254.2/30", "dev", "lan"],
        ["ip", "-n", NS, "link", "set", "lan", "up"],
        ["ip", "-n", NS, "route", "add", f"{tailnet}/32", "via", "10.254.254.1"],
    ):
        run(command, show=True)
    ping = ["ip", "netns", "exec", NS, "ping", "-c", "1", "-W", "2", "10.254.254.1"]
    wall.reaches("stand-in LAN machine reaches Beelink", lambda: wall.shown(*ping))
    for target in published:
        wall.stopped(f"LAN route to {target}", "forward", lambda t=target: wall.from_lan(t))

    if wall.failures:
        print(f"WALL TEST FAILED ({wall.failures})")
        return 1
    print("WALL TEST PASSED")
    return 0


def main(host: Host | None = None) -> int:
    host = host or Host()
    if not host.is_root():
        print("run with sudo", file=sys.stderr)
        return 1
    cleanup(host)
    try:
        return wall_test(host)
    finally:
        cleanup(host)


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
