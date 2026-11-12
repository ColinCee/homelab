#!/bin/sh
# The work is in run_vm.py. -E and -s, as root, ignore PYTHON* variables and user packages; not -I,
# which would drop this folder, and with it vm_disks.py, from the import path.
exec /usr/local/bin/homelab-python3 -Es "$(dirname "$0")/run_vm.py" "$@"
