# Slot counts and role for install-host.sh, which sources this file. Each comes from its variable if given, else
# the host's file in $etc from the last run (install-host.sh writes them). Tests set etc to a folder of their own.
# shellcheck shell=bash
etc=${etc:-/etc/homelab-ci}

# The Beelink's light slots never fall below this, so a merge that raises it raises the host's count (update.sh
# reruns install-host.sh for any change under ci-runner/); a higher count is kept. Sized for the 16 GiB host: 3 GiB
# stays reserved for the home's services and 4 build slots take up to 2 + 2x4 = 10 GiB on paper, so 10 light VMs of
# 1-2 GiB are over on paper; they mostly wait on the network and touch far less, and the supervisor's MemAvailable
# check holds any VM back before the reserve is reached.
LIGHT_SLOTS_MIN=10

# How many VMs run at once: 5 when not a number, at least 1, as in ci_supervisor.py.
SLOTS=${SLOTS:-$(cat "$etc/slots" 2>/dev/null || true)}
SLOTS=${SLOTS//[[:space:]]/}
if [[ $SLOTS =~ ^[0-9]+$ ]]; then SLOTS=$((10#$SLOTS > 0 ? 10#$SLOTS : 1)); else SLOTS=5; fi
# Light slots (small VMs for light jobs, numbered after the build slots); 0 is none.
LIGHT_SLOTS=${LIGHT_SLOTS:-$(cat "$etc/light-slots" 2>/dev/null || true)}
LIGHT_SLOTS=${LIGHT_SLOTS//[[:space:]]/}
if [[ $LIGHT_SLOTS =~ ^[0-9]+$ ]]; then LIGHT_SLOTS=$((10#$LIGHT_SLOTS)); else LIGHT_SLOTS=5; fi
# ROLE=pc on the PC's Hyper-V guest: ready runners registered through the Beelink's door
# (ci_supervisor.py pc), no GitHub token, no light slots.
ROLE=${ROLE:-$(cat "$etc/role" 2>/dev/null || echo beelink)}
[[ $ROLE == beelink || $ROLE == pc ]] || { echo "ROLE is beelink or pc" >&2; exit 2; }
if [[ $ROLE == pc ]]; then
  LIGHT_SLOTS=0
elif ((LIGHT_SLOTS < LIGHT_SLOTS_MIN)); then
  LIGHT_SLOTS=$LIGHT_SLOTS_MIN
fi
# On the Beelink, the door for the PC's slots ("PC build slots" in ci-runner.md): at most this many runners, 0 closed.
PEER_SLOTS=${PEER_SLOTS:-$(cat "$etc/peer-slots-max" 2>/dev/null || true)}
PEER_SLOTS=${PEER_SLOTS//[[:space:]]/}
[[ $PEER_SLOTS =~ ^[0-9]+$ && $ROLE == beelink ]] && PEER_SLOTS=$((10#$PEER_SLOTS)) || PEER_SLOTS=0
