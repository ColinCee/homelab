"""socket_users.py finds every container that mounts Docker's socket."""

from __future__ import annotations

from socket_users import SOCKET, stale

from conftest import services


def test_every_stack_mounts_the_socket_where_the_restart_looks() -> None:
    mounts = [
        str(volume)
        for _stack, _name, service in services()
        for volume in service.get("volumes", [])
        if "docker.sock" in str(volume)
    ]
    assert mounts, "no stack mounts the socket: drop socket_users.py and its drop-in"
    assert all(m.split(":")[1] == SOCKET for m in mounts), mounts


def test_only_a_container_started_before_the_socket_is_restarted() -> None:
    # The socket remade at 06:32:40 on 17 November 2026; the proxy started the day before.
    made = 1794897160.0
    started = "proxy 2026-11-16T12:09:57.123456789Z\nnew 2026-11-17T06:33:01.5Z\n"
    assert stale(started, made) == ["proxy"]
