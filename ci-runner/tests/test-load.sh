#!/usr/bin/env bash
# sudo ci-runner/tests/test-load.sh [SECONDS]: the load test at every slot, in test_load.py.
exec python3 "$(dirname "$0")/test_load.py" "$@"
