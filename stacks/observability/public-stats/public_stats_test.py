import io
import json
import logging
import urllib.request
from pathlib import Path
from typing import Any

import public_stats
import public_stats_key
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from public_stats import STACKS, StatsError, build_body, signed

from conftest import stacks as compose_stacks

NOW = 1_800_000_000.0


def results(running: tuple[str, ...] = STACKS) -> dict[str, Any]:
    return {
        "running": [
            {"metric": {"container_label_com_docker_compose_project": s}, "value": [NOW, "1"]}
            for s in running
        ],
    }


KPIS = {"trackedAircraft": 237, "throughputLast60Min": 107, "apiHealth": "live"}


def test_stacks_match_the_repository() -> None:
    assert [stack.name for stack in compose_stacks()] == list(STACKS)


def test_body_holds_only_named_numbers() -> None:
    assert build_body(results(), KPIS, NOW) == {
        "at": int(NOW),
        "allUp": True,
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


@pytest.mark.parametrize("tracked", ["x", 10_000_000])
def test_odd_flight_data_leaves_the_flight_numbers_empty(tracked: object) -> None:
    body = build_body(results(), {**KPIS, "trackedAircraft": tracked}, NOW)
    assert body["tracking"] is None


def test_the_signature_covers_exactly_the_bytes_sent() -> None:
    key = Ed25519PrivateKey.generate()
    data, signature = signed(key, build_body(results(), KPIS, NOW))
    key.public_key().verify(bytes.fromhex(signature), data)


def test_a_login_logs_in_to_the_prometheus_api_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sent: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        sent.append(request)
        prometheus = "/api/v1/query" in request.full_url
        answer = {"status": "success", "data": {"result": results()["running"]}}
        return io.BytesIO(json.dumps(answer if prometheus else {"kpis": KPIS}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    public_stats.gather("http://gateway:5080/prometheus", "https://flights")
    assert all(r.get_header("Authorization") is None for r in sent)
    (tmp_path / "stats_password").write_text("pw", encoding="utf-8")
    monkeypatch.setenv("PROMETHEUS_USER", "stats@homelab.internal")
    monkeypatch.setenv("PROMETHEUS_PASSWORD_FILE", str(tmp_path / "stats_password"))
    sent.clear()
    body = public_stats.gather("http://gateway:5080/prometheus", "https://flights")
    assert body["allUp"]
    query, flights = sent
    assert query.unredirected_hdrs["Authorization"] == "Basic c3RhdHNAaG9tZWxhYi5pbnRlcm5hbDpwdw=="
    assert flights.get_header("Authorization") is None


def test_a_rotated_key_is_the_file_the_service_reads(tmp_path: Path) -> None:
    out = tmp_path / "public_stats_key"
    public = public_stats_key.main(out)
    # As public_stats.main loads it, and the blog checks it with the printed half.
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(out.read_text().strip()))
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(public)).verify(key.sign(b"x"), b"x")


def test_a_push_failing_through_a_restart_warns_and_one_failing_for_minutes_is_an_error() -> None:
    # Pushes every 5 minutes, then a restart fails two tries 15 s apart, well after the last push.
    failures = public_stats.Failures()
    failures.ok()
    assert failures.level(1000) == logging.WARNING
    assert failures.level(1015) == logging.WARNING
    failures.ok()
    # Failing on and on: an error once the first failure in a row is FAILING_S old.
    assert failures.level(2000) == logging.WARNING
    assert failures.level(2000 + public_stats.FAILING_S) == logging.ERROR
