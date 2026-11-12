#!/bin/sh
# The work is in stop_vms.py; why -Es: run-vm.sh.
exec /usr/bin/python3 -Es "$(dirname "$0")/stop_vms.py" "$@"
