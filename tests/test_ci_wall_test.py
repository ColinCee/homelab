"""The container wall test (ci-runner/host-tests/test_wall.py): a PASS needs the wall's drop
counter to rise, and the throwaway network and namespace go whatever happens."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "wall_test_script", Path(__file__).parents[1] / "ci-runner/host-tests/test_wall.py"
)
assert SPEC is not None and SPEC.loader is not None
wall = importlib.util.module_from_spec(SPEC)
sys.modules["wall_test_script"] = wall
SPEC.loader.exec_module(wall)

TAILNET = "100.64.0.5"
PORTS = f"{TAILNET}:8123->8123/tcp, 0.0.0.0:80->80/tcp\n{TAILNET}:5080->3000/tcp\n"


def chain(packets: int) -> str:
    return json.dumps(
        {
            "nftables": [
                {"rule": {"expr": [{"counter": {"packets": packets}}, {"drop": None}]}},
                {"rule": {"expr": [{"counter": {"packets": 1}}, {"accept": None}]}},
            ]
        }
    )


class FakeHost:
    """Every probe fails, and each one the wall should stop counts a drop, unless nft
    cannot list the COUNTERLESS chain or the SILENT chain never counts; a target in
    REACHED connects."""

    def __init__(
        self, reached: frozenset[str] = frozenset(), counterless: str = "", silent: str = ""
    ) -> None:
        self.ran: list[list[str]] = []
        self.drops = {"from_container": 0, "forward": 0}
        self.reached = reached
        self.counterless = counterless
        self.silent = silent
        self.ports = PORTS

    def run(
        self, command: list[str], *, show: bool = False, quiet: bool = False
    ) -> subprocess.CompletedProcess[str]:
        self.ran.append(command)
        code, out = 0, ""
        match command:
            case ["ip", "-4", "-br", "addr", "show", "tailscale0"]:
                out = f"tailscale0 UNKNOWN {TAILNET}/32 fd7a::5/128\n"
            case ["ip", "-4", "route", "show", "default"]:
                out = "default via 192.168.1.1 dev enp1s0 proto dhcp\n"
            case ["nft", "list", "set", "inet", "homelab_wall", "wan4"]:
                out = "table inet homelab_wall {\n  set wan4 {\n    elements = { 203.0.113.9 }\n"
            case ["docker", "ps", *_]:
                out = self.ports
            case ["nft", "-j", "list", "chain", "inet", "homelab_wall", name]:
                code = 1 if name == self.counterless else 0
                out = chain(self.drops[name])
            case ["docker", "run", "--rm", "--network", _, _, "nc", "-w", "3", host, port]:
                code = self.probe("from_container", f"{host}:{port}")
            case ["ip", "netns", "exec", _, "timeout", "3", "bash", "-c", tcp]:
                host, port = tcp.removeprefix("exec 3<>/dev/tcp/").split("/")
                code = self.probe("forward", f"{host}:{port}")
        return subprocess.CompletedProcess(command, code, out)

    def probe(self, chain_name: str, target: str) -> int:
        allowed = ("1.1.1.1:443", "homelab-wall-peer:8080")
        if target in self.reached or target in allowed:
            return 0
        if chain_name != self.silent:
            self.drops[chain_name] += 1
        return 1

    def host(self) -> object:
        return wall.Host(run=self.run, sleep=lambda _: None, is_root=lambda: True)

    def cleaned_up_first_and_last(self) -> bool:
        return self.ran[0] == ["docker", "rm", "-f", "homelab-wall-peer"] and self.ran[-1] == [
            "ip",
            "link",
            "del",
            "homelab-wall0",
        ]


def test_a_wall_that_drops_every_private_route_passes(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeHost()
    assert wall.main(fake.host()) == 0
    out = capsys.readouterr().out
    assert f"PASS container to {TAILNET}:5080: dropped by the wall" in out
    assert "PASS container to 192.168.1.1:80: dropped by the wall" in out
    assert "PASS container to 203.0.113.9:443: dropped by the wall" in out
    assert f"PASS LAN route to {TAILNET}:8123: dropped by the wall" in out
    assert "0.0.0.0:80" not in out
    assert out.endswith("WALL TEST PASSED\n")
    assert fake.cleaned_up_first_and_last()


def test_a_reached_route_fails(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeHost(reached=frozenset({"100.100.100.100:53"}))
    assert wall.main(fake.host()) == 1
    out = capsys.readouterr().out
    assert "FAIL container to 100.100.100.100:53: reached" in out
    assert out.endswith("WALL TEST FAILED (1)\n")


@pytest.mark.parametrize("fault", ["counterless", "silent"])
def test_a_probe_failing_without_a_counted_drop_fails(
    fault: str, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeHost(counterless="forward") if fault == "counterless" else FakeHost(silent="forward")
    assert wall.main(fake.host()) == 1
    out = capsys.readouterr().out
    assert f"FAIL LAN route to {TAILNET}:8123: failed, but not by the wall" in out
    assert "WALL TEST FAILED (2)" in out


def test_nothing_published_fails_and_still_cleans_up(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeHost()
    fake.ports = "0.0.0.0:80->80/tcp\n"
    assert wall.main(fake.host()) == 1
    assert f"FAIL nothing is published on {TAILNET} to test against" in capsys.readouterr().out
    assert fake.cleaned_up_first_and_last()


def test_drops_sums_every_counter_in_the_chain() -> None:
    assert wall.counted(json.loads(chain(4))) == 5
    assert wall.counted({"nftables": [{"metainfo": {"version": "1.0"}}]}) == 0


def test_a_missing_program_fails_like_a_shell() -> None:
    assert wall.run_command(["/nonexistent/nc"], quiet=True).returncode == 127
