"""The .internal names check (stacks/observability/probes.py) passes only on an answer that gives
the Beelink's address."""

import socket
import struct
import threading
from collections.abc import Iterator

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
    yield sock.getsockname()[1]
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
