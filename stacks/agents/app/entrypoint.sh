#!/bin/bash
set -e

# Ensure volume directories are writable by agent user
# (named volumes are created as root on first mount)
chown agent:agent /repo.git /reviews

exec runuser -u agent -- "$@"
