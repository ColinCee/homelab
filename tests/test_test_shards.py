"""The test runner (scripts/test_shards.py): balanced shares and one slowest-tests list."""

import json
from pathlib import Path

from scripts import test_shards


def test_the_shares_are_balanced_and_hold_every_file_once() -> None:
    files = [Path(f"tests/test_{n}.py") for n in range(10)] + [Path("tests/test_update.py")]
    shares = test_shards.shares(files, 4)
    assert sorted(f for share in shares for f in share) == sorted(files)
    weights = [sum(test_shards.HEAVY.get(f.name, test_shards.LIGHT) for f in s) for s in shares]
    assert max(weights) - min(weights) <= test_shards.HEAVY["test_update.py"]
    # More cores than files leaves no empty share.
    assert test_shards.shares(files[:2], 8) == [[files[0]], [files[1]]]


def test_the_slowest_tests_merge_slowest_first_within_the_annotation_size() -> None:
    a = json.dumps([["a::slow", 1.0, 3.0], ["a::quick", 2.0, 0.1]])
    b = json.dumps([["b::middle", 1.5, 1.0]] + [[f"b::{n}" * 30, 0.0, 0.01] for n in range(100)])
    merged = json.loads(test_shards.merge_times([a, b, ""]))
    assert [r[0] for r in merged[:3]] == ["a::slow", "b::middle", "a::quick"]
    assert len(json.dumps(merged, separators=(",", ":"))) <= test_shards.TIMES_CHARS
