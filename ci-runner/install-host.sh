#!/bin/sh
# The stable path to install_host.py, which the docs and older update.sh copies run. -I keeps Python from reading
# PYTHON* variables, as it runs as root.
exec python3 -I "$(dirname "$0")/install_host.py" "$@"
