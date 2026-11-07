#!/usr/bin/env bash
# Each host takes homelab's main within a minute on its own (homelab-ci-update.timer). This runs that pass now, from
# Colin's PC, and shows what it did: the Beelink first, since the PC follows the commit and image it runs.
set -euo pipefail
guest=(ssh -i D:/kaizen-ci/ssh/id_ed25519 -o UserKnownHostsFile=D:/kaizen-ci/ssh/known_hosts -o ConnectTimeout=10
  kaizen@192.168.250.10)
# shellcheck disable=SC2016 # expands on the host, on purpose
pass='u=homelab-ci-update.service; sudo systemctl start $u; s=$?;
  sudo journalctl -o cat --no-pager _SYSTEMD_INVOCATION_ID=$(systemctl show -p InvocationID --value $u); exit $s'

# shellcheck disable=SC2029 # a fixed command, built here on purpose
ssh colin@beelink "$pass" 2>&1 | sed 's/^/beelink: /' || echo "beelink: the pass failed; the PC still runs its own"
if "${guest[@]}" true 2>/dev/null; then
  "${guest[@]}" "$pass" 2>&1 | sed 's/^/pc: /'
else
  echo "pc: off; it catches up once it is on"
fi
