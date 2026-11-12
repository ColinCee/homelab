#!/bin/sh
# The work is in stop_vms.py; why -Es: run-vm.sh.
exec /usr/local/bin/homelab-python3 -Es "$(dirname "$0")/stop_vms.py" "$@"
