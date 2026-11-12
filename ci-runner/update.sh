#!/bin/sh
# The stable path to update.py, installed beside it, which homelab-ci-update.service and the docs run. -I keeps
# Python from reading PYTHON* variables, as it runs as root.
exec /usr/bin/python3 -I "$(dirname "$0")/update.py" "$@"
