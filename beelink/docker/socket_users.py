"""Restart each container that mounts Docker's socket, after Docker starts (socket-users.conf).

A Docker update recreates the socket while live-restore keeps the containers running (daemon.json),
so a container that bind-mounts the socket keeps the dead one: on 17 November 2026 the socket proxy
did, and Alloy lost every container's logs and metrics for six hours. A restart mounts the new one.
Installed by ci-runner/install_host.py.
"""

from __future__ import annotations

import subprocess

# Where stacks mount the socket: `--filter volume` matches a bind mount by its path inside.
SOCKET = "/var/run/docker.sock"


def main() -> None:
    ps = ["docker", "ps", "--quiet", "--filter", f"volume={SOCKET}"]
    ids = subprocess.run(ps, check=True, capture_output=True, text=True).stdout.split()
    if ids:
        subprocess.run(["docker", "restart", *ids], check=True)


if __name__ == "__main__":
    main()
