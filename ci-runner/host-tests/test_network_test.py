"""The network test's verdict (ci-runner/host-tests/test_network.py) on network-probe.sh's lines."""

import importlib.util
import json
import sys

import pytest

from conftest import ROOT

SPEC = importlib.util.spec_from_file_location(
    "network_test_script", ROOT / "ci-runner/host-tests/test_network.py"
)
assert SPEC is not None and SPEC.loader is not None
network = importlib.util.module_from_spec(SPEC)
sys.modules["network_test_script"] = network
SPEC.loader.exec_module(network)

PASSED = "boot\r\nPASS ok github api\r\nPASS blocked 10.200.0.1:22\r\nTOTAL 2\r\nLISTENING\r\n"


def test_every_check_passed_and_inbound_blocked_passes() -> None:
    results, failed = network.verdict(PASSED + "INBOUND_NONE\r\n", "blocked")
    assert failed is None and len(results) == 2


@pytest.mark.parametrize(
    ("console", "inbound", "line"),
    [
        (
            PASSED.replace("TOTAL 2", "TOTAL 3"),
            "blocked",
            "2 of 3 passed, 0 failed, inbound blocked",
        ),
        (PASSED.replace("TOTAL 2\r\n", ""), "blocked", "2 of ? passed, 0 failed, inbound blocked"),
        (PASSED + "FAIL blocked ping host (got ok)\n", "blocked", "2 of 2 passed, 1 failed"),
        (PASSED, "not dropped by our rule", "inbound not dropped by our rule"),
        (PASSED + "INBOUND_CONNECTED\r\n", "blocked", "inbound reached"),
    ],
)
def test_a_missed_check_or_an_open_inbound_fails(console: str, inbound: str, line: str) -> None:
    _, failed = network.verdict(console, inbound)
    assert failed is not None and line in failed


def test_inbound_drops_reads_the_counter_of_the_rule_on_the_vms_interface() -> None:
    chain = {
        "nftables": [
            {"metainfo": {}},
            {"rule": {"expr": [{"match": {"left": "iifname"}}, {"counter": {"packets": 9}}]}},
            {"rule": {"expr": [{"match": {"left": "oifname"}}, {"counter": {"packets": 4}}]}},
        ]
    }
    assert network.inbound_drops(json.dumps(chain)) == 4
    assert network.inbound_drops(json.dumps({"nftables": []})) == 0
