"""The test runner (checks/test_shards.py): balanced shares and one slowest-tests list."""

import json
import re
from pathlib import Path

from checks import test_shards
from conftest import tracked


def test_the_shares_are_balanced_and_hold_every_file_once() -> None:
    files = [Path(f"tests/test_{n}.py") for n in range(10)] + [Path("ci-runner/update_test.py")]
    shares = test_shards.shares(files, 4)
    assert sorted(f for share in shares for f in share) == sorted(files)
    weights = [sum(test_shards.HEAVY.get(f.name, test_shards.LIGHT) for f in s) for s in shares]
    assert max(weights) - min(weights) <= test_shards.HEAVY["update_test.py"]
    # More cores than files leaves no empty share.
    assert test_shards.shares(files[:2], 8) == [[files[0]], [files[1]]]


def test_the_slowest_tests_merge_slowest_first_within_the_annotation_size() -> None:
    a = json.dumps([["a::slow", 1.0, 3.0], ["a::quick", 2.0, 0.1]])
    b = json.dumps([["b::middle", 1.5, 1.0]] + [[f"b::{n}" * 30, 0.0, 0.01] for n in range(100)])
    merged = json.loads(test_shards.merge_times([a, b, ""]))
    assert [r[0] for r in merged[:3]] == ["a::slow", "b::middle", "a::quick"]
    assert len(json.dumps(merged, separators=(",", ":"))) <= test_shards.TIMES_CHARS


def test_a_shard_a_filter_left_empty_passes_but_any_failure_fails() -> None:
    assert test_shards.verdict([0, 5, 5]) == 0
    assert test_shards.verdict([5, 5]) == 5
    assert test_shards.verdict([0, 1, 5]) == 1
    assert test_shards.verdict([0, -9]) == 137


def test_every_test_module_is_one_the_runner_finds() -> None:
    # The runner takes *_test.py only: tests in a test_*.py would never run. The host's own checks
    # in host-tests/ are run on the host by name, not by pytest.
    stray = [
        p
        for p in tracked(".py")
        if p.name.startswith("test_")
        and not p.name.endswith("_test.py")
        and p.parent.name != "host-tests"
        and re.search(r"^def test_", p.read_text(encoding="utf-8"), re.M)
    ]
    assert stray == []
