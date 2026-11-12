#!/usr/bin/env bash
# setup-net.sh [up|down|wan]: the CI microVM network, in setup_net.py.
exec python3 "$(dirname "$0")/setup_net.py" "$@"
