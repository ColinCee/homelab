from pathlib import Path

import pytest

from stacks.observability.public_stats import STACKS, StatsError, build_body

NOW = 1_800_000_000.0


def results(running: tuple[str, ...] = STACKS) -> dict:
    return {
        "running": [
            {"metric": {"container_label_com_docker_compose_project": s}, "value": [NOW, "1"]}
            for s in running
        ],
        "blocked": [{"metric": {}, "value": [NOW, "1212"]}],
        "ha_started": [{"metric": {}, "value": [NOW, str(NOW - 3.5 * 86400)]}],
        "backup_at": [{"metric": {}, "value": [NOW, str(NOW - 10 * 3600)]}],
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
        "haDays": 3,
        "backupOk": True,
    }


def test_one_stack_down_clears_all_up() -> None:
    assert build_body(results(STACKS[1:]), KPIS, NOW)["allUp"] is False


def test_nothing_reported_is_not_published_as_down() -> None:
    with pytest.raises(StatsError):
        build_body(results(()), KPIS, NOW)


def test_stale_flight_data_and_old_backup_are_not_shown_as_live() -> None:
    old = results()
    old["backup_at"] = [{"metric": {}, "value": [NOW, str(NOW - 30 * 3600)]}]
    body = build_body(old, {**KPIS, "apiHealth": "stale"}, NOW)
    assert (body["tracking"], body["londonHour"], body["backupOk"]) == (None, None, False)
