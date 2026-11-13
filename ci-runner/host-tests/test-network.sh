#!/usr/bin/env bash
# sudo ci-runner/host-tests/test-network.sh: the microVM network rules from inside a VM, in test_network.py.
exec /usr/local/bin/homelab-python3 -Es "$(dirname "$0")/test_network.py" "$@"
