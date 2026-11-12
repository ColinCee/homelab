#!/usr/bin/env python3
"""Bring up or tear down the CI microVM network: one tap per slot, the nftables rules
(homelab-ci.nft, which also holds the container wall), and the iptables accept Docker's
FORWARD chain needs. Idempotent; run by homelab-ci-net.service after Docker.
  setup-net.sh [up|down|wan]
"wan" refreshes the blocked public address; run_vm.py calls it before each VM.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

DEFAULT_SLOTS = 5
IPV4 = re.compile(r"([0-9]{1,3}\.){3}[0-9]{1,3}")
SLOT_USER = re.compile(r"fc-slot[0-9]+")
TRACE_URL = "https://1.1.1.1/cdn-cgi/trace"


class Run(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        input: str | None = None,
        check: bool = True,
        quiet: bool = False,
    ) -> subprocess.CompletedProcess[str]: ...


def run_command(
    command: list[str], *, input: str | None = None, check: bool = True, quiet: bool = False
) -> subprocess.CompletedProcess[str]:
    """Stdout is captured; stderr goes to the journal unless QUIET, for a command whose
    failure is an expected answer rather than an error."""
    return subprocess.run(
        command,
        input=input,
        text=True,
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL if quiet else None,
        check=check,
    )


def slot_count(env: Mapping[str, str], root: Path) -> int:
    """The host's slot count (/etc/homelab-ci/slots, which install-host.sh writes); an
    explicit SLOTS wins. 5 when neither is a number, at least 1, as in ci_supervisor.py."""
    text = env.get("SLOTS", "")
    if not text:
        try:
            text = (root / "etc/homelab-ci/slots").read_text(errors="replace")
        except OSError:
            text = ""
    text = re.sub(r"\s", "", text)
    if re.fullmatch(r"[0-9]+", text, re.ASCII):
        return max(int(text), 1)
    return DEFAULT_SLOTS


class Network:
    def __init__(self, run: Run, root: Path, here: Path, slots: int) -> None:
        self.run = run
        self.root = root
        self.here = here
        self.slots = slots
        # The accept is limited to the uplink, so it opens nothing private even if the
        # nftables table were missing.
        route = run(["ip", "-4", "route", "show", "default"]).stdout.splitlines()
        fields = route[0].split() if route else []
        self.uplink = fields[4] if len(fields) > 4 else ""

    def accept_rules(self) -> list[list[str]]:
        return [
            ["-i", "fc-tap+", "-o", self.uplink, "-j", "ACCEPT"],
            [
                *["-i", self.uplink, "-o", "fc-tap+", "-m", "conntrack"],
                *["--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
            ],
        ]

    def public_address(self) -> str | None:
        # A few tries: one slow answer should not cost a VM (run_vm.py refuses to start without it).
        try:
            trace = self.run(
                [
                    *["curl", "-4", "-sf", "-m", "3", "--retry", "3", "--retry-all-errors"],
                    *["--retry-delay", "1", TRACE_URL],
                ],
                check=False,
            ).stdout
        except FileNotFoundError:
            trace = ""
        wan = "\n".join(line[3:] for line in trace.splitlines() if line.startswith("ip="))
        if not re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3}){3}", wan):
            print("cannot find the public address", file=sys.stderr)
            return None
        return wan

    def refresh_wan(self) -> bool:
        wan = self.public_address()
        if wan is None:
            return False
        # One transaction per table, so a set is never empty in between, and a missing wall
        # never stops VMs from starting.
        if self.nft(wan_elements("homelab_wall", wan, flush=True), check=False):
            print("the container wall is not loaded", file=sys.stderr)
        if self.nft(wan_elements("homelab_ci", wan, flush=True), check=False):
            return False
        listed = self.run(["nft", "list", "set", "inet", "homelab_ci", "wan4"], check=False)
        return listed.returncode == 0 and wan in listed.stdout

    def nft(self, rules: str, *, check: bool = True) -> int:
        return self.run(["nft", "-f", "-"], input=rules, check=check).returncode

    def down(self) -> None:
        for rule in self.accept_rules():
            while (
                self.run(
                    ["iptables", "-D", "DOCKER-USER", *rule], check=False, quiet=True
                ).returncode
                == 0
            ):
                pass
        for line in self.run(["ip", "-br", "link"]).stdout.splitlines():
            fields = line.split()
            if fields and fields[0].startswith("fc-tap"):
                self.run(["ip", "link", "del", fields[0]])
        # The container wall (homelab_wall) stays: it is not the VMs'.
        self.run(["nft", "delete", "table", "inet", "homelab_ci"], check=False, quiet=True)

    def up(self) -> int:
        # The rules load first, with any public address already loaded, so running VMs never
        # see the blocked address set empty and the container wall is up before the slow
        # address lookup. On a first start run_vm.py refuses VMs until the address is set.
        # Every fc-slot user that exists, not the slot count, so a user left from a larger
        # count stays walled off too.
        uids = ",".join(
            fields[2]
            for line in self.run(["getent", "passwd"]).stdout.splitlines()
            if len(fields := line.split(":")) > 2 and SLOT_USER.fullmatch(fields[0])
        )
        resolvers = ",".join(self.resolvers())
        if not resolvers:
            print(
                "no DNS servers in systemd-resolved: builds and default-bridge containers"
                " cannot resolve",
                file=sys.stderr,
            )
        loaded = "".join(
            self.run(["nft", "list", "set", "inet", table, "wan4"], check=False, quiet=True).stdout
            for table in ("homelab_ci", "homelab_wall")
        )
        found = IPV4.search(loaded)
        wan = found.group(0) if found else ""
        rules = (self.here / "homelab-ci.nft").read_text()
        if wan:
            rules += wan_elements("homelab_ci", wan) + wan_elements("homelab_wall", wan)
        if uids:
            rules += f"add element inet homelab_ci slot_uids {{ {uids} }}\n"
        if resolvers:
            rules += f"add element inet homelab_wall resolvers {{ {resolvers} }}\n"
        self.nft(rules)
        if not self.refresh_wan():
            print(
                f"public address not refreshed: {wan or 'none'} kept; run_vm.py refreshes it"
                " before each VM",
                file=sys.stderr,
            )
        if not self.uplink:
            print("no default route", file=sys.stderr)
            return 1

        for n in range(self.slots):
            tap = f"fc-tap{n}"
            if self.run(["ip", "link", "show", tap], check=False, quiet=True).returncode:
                user = f"fc-slot{n}"
                self.run(
                    [
                        *["ip", "tuntap", "add", "dev", tap, "mode", "tap"],
                        *["user", user, "group", user],
                    ]
                )
            self.run(["sysctl", "-q", "-w", f"net.ipv6.conf.{tap}.disable_ipv6=1"])
            self.run(["ip", "addr", "replace", f"10.200.{n}.1/30", "dev", tap])
            self.run(["ip", "link", "set", tap, "up"])

        for rule in self.accept_rules():
            if self.run(
                ["iptables", "-C", "DOCKER-USER", *rule], check=False, quiet=True
            ).returncode:
                self.run(["iptables", "-I", "DOCKER-USER", *rule])
        return 0

    def resolvers(self) -> list[str]:
        try:
            text = (self.root / "run/systemd/resolve/resolv.conf").read_text(errors="replace")
        except OSError:
            return []
        return [
            fields[1]
            for line in text.splitlines()
            if len(fields := line.split()) > 1
            and fields[0] == "nameserver"
            and re.fullmatch(r"[0-9.]+", fields[1])
        ]


def wan_elements(table: str, wan: str, *, flush: bool = False) -> str:
    """The public address goes in each table's wan4 (homelab-ci.nft)."""
    lines = f"flush set inet {table} wan4\n" if flush else ""
    return lines + f"add element inet {table} wan4 {{ {wan} }}\n"


def main(
    argv: list[str],
    *,
    run: Run = run_command,
    root: Path = Path("/"),
    env: Mapping[str, str] = os.environ,
    here: Path | None = None,
) -> int:
    here = here or Path(__file__).resolve().parent
    mode = (argv[0] if argv else "") or "up"
    if mode not in ("up", "down", "wan"):
        print("usage: setup-net.sh [up|down|wan]", file=sys.stderr)
        return 2
    try:
        network = Network(run, root, here, slot_count(env, root))
        if mode == "wan":
            return 0 if network.refresh_wan() else 1
        if mode == "down":
            network.down()
            return 0
        return network.up()
    except subprocess.CalledProcessError as e:
        return e.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
