#!/usr/bin/env bash
# sudo ci-runner/tests/audit.sh: the CI runner's blast-radius audit, in audit.py.
exec /usr/bin/python3 -Es "$(dirname "$0")/audit.py" "$@"
