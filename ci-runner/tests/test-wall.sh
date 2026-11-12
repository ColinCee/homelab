#!/usr/bin/env bash
# sudo ci-runner/tests/test-wall.sh: the container wall, in test_wall.py.
exec /usr/local/bin/homelab-python3 -Es "$(dirname "$0")/test_wall.py" "$@"
