#!/usr/bin/env bash
# The Beelink takes homelab's main within a minute on its own (homelab-ci-update.timer). This runs that pass now and
# shows what it did.
set -euo pipefail
# shellcheck disable=SC2016 # expands on the host, on purpose
pass='u=homelab-ci-update.service; sudo systemctl start $u; s=$?;
  sudo journalctl -o cat --no-pager _SYSTEMD_INVOCATION_ID=$(systemctl show -p InvocationID --value $u); exit $s'

# shellcheck disable=SC2029 # a fixed command, built here on purpose
ssh colin@beelink "$pass"
