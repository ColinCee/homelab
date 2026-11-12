"""The CI VMs' network (ci-runner/net/setup_net.py): taps, rules and the accept, against a
fake host that keeps the taps and iptables rules the commands make."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "setup_net", Path(__file__).parents[1] / "ci-runner/net/setup_net.py"
)
assert SPEC is not None and SPEC.loader is not None
net = importlib.util.module_from_spec(SPEC)
sys.modules["setup_net"] = net
SPEC.loader.exec_module(net)

RULES = "table inet homelab_ci {}\ntable inet homelab_wall {}\n"
PASSWD = "root:x:0:0::/root:/bin/bash\nfc-slot0:x:990:990::/:/usr/sbin/nologin\n"
PASSWD += "fc-slot1:x:991:991::/:/usr/sbin/nologin\nfc-slotx:x:5:5::/:/bin/sh\n"


class FakeHost:
    def __init__(self, route: str = "default via 192.168.1.1 dev enp1s0 proto dhcp\n") -> None:
        self.route = route
        self.taps: set[str] = set()
        self.accepts: list[tuple[str, ...]] = []
        self.wan = "203.0.113.5"
        self.public = "203.0.113.7"
        self.loaded: list[str] = []
        self.commands: list[list[str]] = []

    def __call__(
        self, command: list[str], *, input: str | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        code, out = self.answer(command, input)
        if check and code:
            raise subprocess.CalledProcessError(code, command)
        return subprocess.CompletedProcess(command, code, out)

    def answer(self, command: list[str], input: str | None) -> tuple[int, str]:
        match command:
            case ["ip", "-4", "route", "show", "default"]:
                return 0, self.route
            case ["getent", "passwd"]:
                return 0, PASSWD
            case ["nft", "list", "set", "inet", _, "wan4"]:
                return 0, f"set wan4 {{ elements = {{ {self.wan} }} }}\n"
            case ["nft", "-f", "-"]:
                assert input is not None
                self.loaded.append(input)
                if f"{{ {self.public} }}" in input:
                    self.wan = self.public
                return 0, ""
            case ["curl", *_]:
                return 0, f"fl=1\nip={self.public}\nts=2\n"
            case ["ip", "link", "show", tap]:
                return (0 if tap in self.taps else 1), ""
            case ["ip", "tuntap", "add", "dev", tap, *_]:
                self.taps.add(tap)
                return 0, ""
            case ["ip", "-br", "link"]:
                return 0, "lo UNKNOWN\n" + "".join(f"{t} UP\n" for t in sorted(self.taps))
            case ["ip", "link", "del", tap]:
                self.taps.remove(tap)
                return 0, ""
            case ["sysctl", *_] | ["ip", "addr", "replace", *_] | ["ip", "link", "set", *_]:
                return 0, ""
            case ["iptables", flag, "DOCKER-USER", *rule]:
                there = tuple(rule) in self.accepts
                if flag == "-I":
                    self.accepts.insert(0, tuple(rule))
                elif flag == "-D" and there:
                    self.accepts.remove(tuple(rule))
                return (0 if flag == "-I" or there else 1), ""
            case ["nft", "delete", "table", "inet", "homelab_ci"]:
                return 0, ""
        raise AssertionError(f"unexpected command {command}")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "etc/homelab-ci").mkdir(parents=True)
    (tmp_path / "etc/homelab-ci/slots").write_text("3\n")
    (tmp_path / "run/systemd/resolve").mkdir(parents=True)
    (tmp_path / "run/systemd/resolve/resolv.conf").write_text(
        "nameserver 192.168.1.1\nnameserver fe80::1\nsearch home\n"
    )
    (tmp_path / "homelab-ci.nft").write_text(RULES)
    return tmp_path


def setup(host: FakeHost, root: Path, mode: str, env: dict[str, str] | None = None) -> int:
    return net.main([mode], run=host, root=root, env=env or {}, here=root)


def test_up_makes_a_tap_per_slot_owned_by_its_user(root: Path) -> None:
    host = FakeHost()
    assert setup(host, root, "up") == 0
    assert host.taps == {"fc-tap0", "fc-tap1", "fc-tap2"}
    tuntap = "ip tuntap add dev fc-tap2 mode tap user fc-slot2 group fc-slot2"
    assert tuntap.split() in host.commands
    assert ["ip", "addr", "replace", "10.200.2.1/30", "dev", "fc-tap2"] in host.commands
    assert ["sysctl", "-q", "-w", "net.ipv6.conf.fc-tap2.disable_ipv6=1"] in host.commands
    assert ["ip", "link", "set", "fc-tap2", "up"] in host.commands


def test_up_loads_the_rules_with_the_kept_address_every_slot_user_and_the_resolvers(
    root: Path,
) -> None:
    host = FakeHost()
    setup(host, root, "up")
    first = host.loaded[0]
    assert first.startswith(RULES)
    # The address already loaded, so the set is never empty while the lookup runs.
    assert "add element inet homelab_ci wan4 { 203.0.113.5 }\n" in first
    assert "add element inet homelab_wall wan4 { 203.0.113.5 }\n" in first
    assert "add element inet homelab_ci slot_uids { 990,991 }\n" in first
    assert "add element inet homelab_wall resolvers { 192.168.1.1 }\n" in first
    # Then the fresh address, each table in a transaction of its own.
    assert host.loaded[1:] == [
        "flush set inet homelab_wall wan4\nadd element inet homelab_wall wan4 { 203.0.113.7 }\n",
        "flush set inet homelab_ci wan4\nadd element inet homelab_ci wan4 { 203.0.113.7 }\n",
    ]


def test_up_accepts_forwarding_only_between_the_taps_and_the_uplink(root: Path) -> None:
    host = FakeHost()
    setup(host, root, "up")
    replies = ("-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED")
    assert sorted(host.accepts) == [
        ("-i", "enp1s0", "-o", "fc-tap+", *replies, "-j", "ACCEPT"),
        ("-i", "fc-tap+", "-o", "enp1s0", "-j", "ACCEPT"),
    ]


def test_up_again_changes_nothing_it_made(root: Path) -> None:
    host = FakeHost()
    setup(host, root, "up")
    host.commands.clear()
    assert setup(host, root, "up") == 0
    assert not [c for c in host.commands if c[:3] == ["ip", "tuntap", "add"]]
    assert ["iptables", "-I"] not in [c[:2] for c in host.commands]
    assert len(host.accepts) == 2 and len(host.taps) == 3


def test_down_removes_the_taps_the_accepts_and_the_vm_table_but_not_the_wall(
    root: Path,
) -> None:
    host = FakeHost()
    setup(host, root, "up")
    host.accepts += host.accepts  # a rule added twice is removed twice
    assert setup(host, root, "down") == 0
    assert host.taps == set() and host.accepts == []
    assert ["nft", "delete", "table", "inet", "homelab_ci"] in host.commands
    assert not [c for c in host.commands if "homelab_wall" in c and "delete" in c]


def test_without_a_default_route_the_rules_load_but_no_tap_is_made(root: Path) -> None:
    host = FakeHost(route="")
    assert setup(host, root, "up") == 1
    assert host.loaded and host.taps == set() and host.accepts == []


def test_wan_fails_when_the_public_address_cannot_be_found(root: Path) -> None:
    host = FakeHost()
    host.public = "not an address"
    assert setup(host, root, "wan") == 1
    assert host.loaded == []


def test_a_failing_command_ends_up_with_its_exit_code(root: Path) -> None:
    class RulesRefused(FakeHost):
        def answer(self, command: list[str], input: str | None) -> tuple[int, str]:
            return (3, "") if command == ["nft", "-f", "-"] else super().answer(command, input)

    host = RulesRefused()
    assert setup(host, root, "up") == 3
    assert host.taps == set()


def test_an_unknown_mode_is_a_usage_error(root: Path) -> None:
    assert setup(FakeHost(), root, "sideways") == 2


@pytest.mark.parametrize(
    ("env", "file", "slots"),
    [
        ({"SLOTS": "2"}, "4\n", 2),
        ({}, " 4\n", 4),
        ({"SLOTS": ""}, "4", 4),
        ({}, "0\n", 1),
        ({}, "four\n", 5),
        ({}, None, 5),
        ({"SLOTS": "٣"}, "4", 5),  # a digit outside ASCII is not a number here, as in bash
    ],
)
def test_slot_count(tmp_path: Path, env: dict[str, str], file: str | None, slots: int) -> None:
    if file is not None:
        (tmp_path / "etc/homelab-ci").mkdir(parents=True)
        (tmp_path / "etc/homelab-ci/slots").write_text(file)
    assert net.slot_count(env, tmp_path) == slots
