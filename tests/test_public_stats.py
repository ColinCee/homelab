from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from stacks.observability.public_stats import STACKS, StatsError, build_body, signed

NOW = 1_800_000_000.0


def results(running: tuple[str, ...] = STACKS) -> dict:
    return {
        "running": [
            {"metric": {"container_label_com_docker_compose_project": s}, "value": [NOW, "1"]}
            for s in running
        ],
        "blocked": [{"metric": {}, "value": [NOW, "1212"]}],
    }


KPIS = {"trackedAircraft": 237, "throughputLast60Min": 107, "apiHealth": "live"}


def test_stacks_match_the_repository() -> None:
    stacks = Path(__file__).parent.parent / "stacks"
    assert sorted(p.parent.name for p in stacks.glob("*/compose.yaml")) == list(STACKS)


def test_body_holds_only_named_numbers() -> None:
    assert build_body(results(), KPIS, NOW) == {
        "at": int(NOW),
        "allUp": True,
        "blocked": 1212,
        "tracking": 237,
        "londonHour": 107,
    }


def test_one_stack_down_clears_all_up() -> None:
    assert build_body(results(STACKS[1:]), KPIS, NOW)["allUp"] is False


def test_nothing_reported_is_not_published_as_down() -> None:
    with pytest.raises(StatsError):
        build_body(results(()), KPIS, NOW)


def test_stale_flight_data_is_not_shown_as_live() -> None:
    old = results()
    body = build_body(old, {**KPIS, "apiHealth": "stale"}, NOW)
    assert (body["tracking"], body["londonHour"]) == (None, None)


@pytest.mark.parametrize(
    "kpis",
    [
        None,
        {"apiHealth": "live"},
        {**KPIS, "trackedAircraft": None},
        {**KPIS, "throughputLast60Min": "x"},
        {**KPIS, "trackedAircraft": 10_000_000},
    ],
)
def test_odd_flight_data_leaves_the_flight_numbers_empty(kpis: dict | None) -> None:
    body = build_body(results(), kpis, NOW)
    assert body["blocked"] == 1212
    assert body["tracking"] in (None, 237)


@pytest.mark.parametrize("value", [[], [{"metric": {}, "value": [NOW, "+Inf"]}]])
def test_no_crowdsec_count_skips_the_push(value: list) -> None:
    odd = results()
    odd["blocked"] = value
    with pytest.raises(StatsError):
        build_body(odd, KPIS, NOW)


def test_the_signature_covers_exactly_the_bytes_sent() -> None:
    key = Ed25519PrivateKey.generate()
    data, signature = signed(key, build_body(results(), KPIS, NOW))
    key.public_key().verify(bytes.fromhex(signature), data)
