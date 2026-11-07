#!/usr/bin/env bash
# Each host takes homelab's main within a minute on its own (homelab-ci-update.timer). This runs that pass now, from
# Colin's PC, and shows what it did: the Beelink first, since the PC follows the commit and image it runs.
set -euo pipefail
guest=(ssh -i D:/kaizen-ci/ssh/id_ed25519 -o UserKnownHostsFile=D:/kaizen-ci/ssh/known_hosts -o ConnectTimeout=10
  kaizen@192.168.250.10)
pass='sudo systemctl start homelab-ci-update.service; s=$?;
  sudo journalctl -u homelab-ci-update.service -o cat --no-pager -n 20 --since "-30min" | tail -n 5; exit $s'

ssh colin@beelink "$pass" 2>&1 | sed 's/^/beelink: /'
if "${guest[@]}" true 2>/dev/null; then
  "${guest[@]}" "$pass" 2>&1 | sed 's/^/pc: /'
else
  echo "pc: off; it catches up once it is on"
fi
