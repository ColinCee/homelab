import io
import json
import re
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


def results(running: tuple[str, ...] = tuple(STACKS)) -> dict[str, Any]:
    return {
        "running": [
            {"metric": {"container_label_com_docker_compose_project": s}, "value": [NOW, "1"]}
            for s in running
        ],
    }


KPIS = {"trackedAircraft": 237, "throughputLast60Min": 107, "apiHealth": "live"}


def deployed(stack: Path, spec: dict[str, Any]) -> tuple[str, ...]:
    """A stack's services a deploy runs: those with no profile, or one its settings.env turns on."""
    settings = stack / "settings.env"
    lines = settings.read_text(encoding="utf-8").splitlines() if settings.exists() else []
    on = {
        profile.strip()
        for line in lines
        if line.startswith("COMPOSE_PROFILES=")
        for profile in line.removeprefix("COMPOSE_PROFILES=").split(",")
    }
    return tuple(
        str(name)
        for name, service in spec["services"].items()
        if not service.get("profiles") or on & set(service["profiles"])
    )


def test_stacks_match_the_repository() -> None:
    repo = {stack.name: deployed(stack, spec) for stack, spec in compose_stacks().items()}
    # On a failure, paste this into public_stats.py's STACKS.
    assert repo == STACKS, f"STACKS = {repo!r}"


# The blog's Worker (worker/index.ts in ColinCee/astro-blog) refuses a body past these.
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def test_stacks_fit_what_the_blog_accepts() -> None:
    assert len(STACKS) <= 16
    for stack, services in STACKS.items():
        assert NAME.fullmatch(stack), stack
        assert 1 <= len(services) <= 32, stack
        assert all(NAME.fullmatch(service) for service in services), services


def test_stacks_fit_the_blogs_home_lab_figure() -> None:
    # The home page draws a row a stack in a box sized for seven
    # (min-height in src/components/home/window/Figures.astro); an eighth
    # would make it taller than the other projects. Raise that first.
    assert len(STACKS) <= 7


def test_the_largest_body_fits_what_the_blog_accepts() -> None:
    # Its byte limit (MAX_BYTES in worker/stacks.ts), with every number at
    # the most digits the Worker takes.
    body = {**build_body(results(), KPIS, NOW), "tracking": 9_999_999, "londonHour": 9_999_999}
    data, _ = signed(Ed25519PrivateKey.generate(), body)
    assert len(data) <= 1024


def test_body_holds_only_named_fields() -> None:
    assert build_body(results(), KPIS, NOW) == {
        "at": int(NOW),
        "allUp": True,
        "tracking": 237,
        "londonHour": 107,
        "stacks": [{"id": stack, "services": list(services)} for stack, services in STACKS.items()],
    }


def test_one_stack_down_clears_all_up() -> None:
    assert build_body(results(tuple(STACKS)[1:]), KPIS, NOW)["allUp"] is False


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
