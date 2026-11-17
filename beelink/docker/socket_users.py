"""Restart each container that holds an older Docker socket than the one there now, after Docker
starts (socket-users.conf).

A Docker update recreates the socket while live-restore keeps the containers running (daemon.json),
so a container that bind-mounts the socket keeps the dead one: on 17 November 2026 the socket proxy
did, and Alloy lost every container's logs and metrics for six hours. A restart mounts the new one.
One started since the socket was made, as at boot, already has it. Installed by
ci-runner/install_host.py.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime

# Where stacks mount the socket: `--filter volume` matches a bind mount by its path inside.
SOCKET = "/var/run/docker.sock"


def stale(started: str, socket_made: float) -> list[str]:
    """The IDs in `docker inspect`'s `<id> <StartedAt>` lines that started before the socket."""
    found = []
    for line in started.splitlines():
        cid, at = line.split()
        when = datetime.strptime(at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        if when.timestamp() < socket_made:
            found.append(cid)
    return found


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout


def main() -> None:
    ids = docker("ps", "--quiet", "--filter", f"volume={SOCKET}").split()
    if not ids:
        return
    started = docker("inspect", "--format", "{{.Id}} {{.State.StartedAt}}", *ids)
    if old := stale(started, os.stat(SOCKET).st_ctime):
        docker("restart", *old)


if __name__ == "__main__":
    main()
