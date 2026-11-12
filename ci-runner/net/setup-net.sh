#!/usr/bin/env bash
# setup-net.sh [up|down|wan]: the CI microVM network, in setup_net.py.
exec /usr/bin/python3 -Es "$(dirname "$0")/setup_net.py" "$@"
