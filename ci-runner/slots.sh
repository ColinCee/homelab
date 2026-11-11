# The slot count for install-host.sh, which sources this file: SLOTS if given, else the host's file in $etc from
# the last run (install-host.sh writes it). Tests set etc to a folder of their own.
# shellcheck shell=bash
etc=${etc:-/etc/homelab-ci}

# How many VMs run at once: 5 when not a number, at least 1, as in ci_supervisor.py.
SLOTS=${SLOTS:-$(cat "$etc/slots" 2>/dev/null || true)}
SLOTS=${SLOTS//[[:space:]]/}
if [[ $SLOTS =~ ^[0-9]+$ ]]; then SLOTS=$((10#$SLOTS > 0 ? 10#$SLOTS : 1)); else SLOTS=5; fi
