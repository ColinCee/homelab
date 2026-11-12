#!/usr/bin/env bash
# sudo ci-runner/tests/audit.sh: the CI runner's blast-radius audit, in audit.py.
exec /usr/local/bin/homelab-python3 -Es "$(dirname "$0")/audit.py" "$@"
