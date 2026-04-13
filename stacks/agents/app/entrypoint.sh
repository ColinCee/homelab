#!/bin/bash
set -e

# Ensure volume directories are writable by agent user
# (named volumes are created as root on first mount)
chown agent:agent /repo.git /reviews

# Give agent user access to Docker socket if mounted (ADR-011).
# Match the host's docker group GID so agent can spawn worker containers.
if [ -S /var/run/docker.sock ]; then
    DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
    if ! getent group "$DOCKER_GID" > /dev/null 2>&1; then
        groupadd -g "$DOCKER_GID" docker-host
    fi
    DOCKER_GROUP=$(getent group "$DOCKER_GID" | cut -d: -f1)
    usermod -aG "$DOCKER_GROUP" agent
fi

exec runuser -u agent -- "$@"
