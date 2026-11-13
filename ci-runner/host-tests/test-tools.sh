#!/usr/bin/env bash
# sudo ci-runner/host-tests/test-tools.sh: the image's tools in a VM in slot 0, in test_tools.py.
exec /usr/local/bin/homelab-python3 -Es "$(dirname "$0")/test_tools.py" "$@"
