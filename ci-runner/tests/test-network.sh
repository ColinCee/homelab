#!/usr/bin/env bash
# sudo ci-runner/tests/test-network.sh: the microVM network rules from inside a VM, in test_network.py.
exec /usr/bin/python3 -Es "$(dirname "$0")/test_network.py" "$@"
