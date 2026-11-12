#!/usr/bin/env bash
# sudo ci-runner/tests/audit.sh: the CI runner's blast-radius audit, in audit.py.
exec python3 "$(dirname "$0")/audit.py" "$@"
