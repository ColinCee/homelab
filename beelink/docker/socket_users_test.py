"""socket_users.py finds every container that mounts Docker's socket."""

from __future__ import annotations

from socket_users import SOCKET

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
