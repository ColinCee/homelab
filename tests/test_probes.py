"""The probes (stacks/observability/probes.py): the .internal names check passes only on an answer
that gives the Beelink's address, and an unreadable blog leaves out only its own number."""

import socket
import struct
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from stacks.observability import probes


def answer(question: bytes, rcode: int, address: str | None) -> bytes:
    count = 1 if address else 0
    header = question[:2] + struct.pack(">HHHHH", 0x8180 | rcode, 1, count, 0, 0)
    reply = header + question[12:]
    if address:
        reply += b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton(address)
    return reply


@pytest.fixture
def server(request: pytest.FixtureRequest) -> Iterator[int]:
    rcode, address = request.param
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))

    def serve() -> None:
        question, client = sock.recvfrom(512)
        sock.sendto(answer(question, rcode, address), client)

    threading.Thread(target=serve, daemon=True).start()
    yield int(sock.getsockname()[1])
    sock.close()


@pytest.mark.parametrize(
    ("server", "passes"),
    [
        ((0, "100.100.146.119"), True),
        ((0, "10.0.0.1"), False),
        ((3, None), False),
        ((0, None), False),
    ],
    indirect=["server"],
)
def test_the_name_check_needs_the_beelinks_address(server: int, passes: bool) -> None:
    assert probes.resolves("home.internal", "127.0.0.1", "100.100.146.119", port=server) is passes


def test_no_answer_fails() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as quiet:
        quiet.bind(("127.0.0.1", 0))
        port = quiet.getsockname()[1]
        passed = probes.resolves(
            "home.internal", "127.0.0.1", "100.100.146.119", port=port, timeout=0.2
        )
    assert passed is False


@pytest.mark.parametrize(
    ("answer", "published"),
    [
        ({"at": 1794599055, "allUp": True}, 1794599055),
        ({"at": "1794599055"}, None),
        ({"at": True}, None),
        ([], None),
        (None, None),
        (OSError("refused"), None),
        (ValueError("not JSON"), None),
    ],
)
def test_the_blog_numbers_time_is_written_only_when_readable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answer: object, published: int | None
) -> None:
    def blog(url: str, *, timeout: float) -> object:
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(probes.telemetry, "http_json", blog)
    monkeypatch.setattr(probes, "OUT", tmp_path / "probes.prom")
    assert probes.published_at() == published
    probes.write({"home-assistant": True}, 1794599100.0, probes.published_at())
    lines = (tmp_path / "probes.prom").read_text().splitlines()
    assert 'homelab_probe_success{target="home-assistant"} 1' in lines  # the checks still land
    expected = f"homelab_public_stats_published_timestamp_seconds {published}"
    assert (expected in lines) is (published is not None)
