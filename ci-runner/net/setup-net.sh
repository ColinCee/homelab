#!/usr/bin/env bash
# Bring up or tear down the CI microVM network: one tap per slot, the nftables
# rules, and the iptables accept Docker's FORWARD chain needs. Idempotent; run by
# homelab-ci-net.service after Docker.
#   setup-net.sh [up|down|wan]
# "wan" refreshes the blocked public address; run-vm.sh calls it before each VM.
set -euo pipefail

# The host's slot count (/etc/homelab-ci/slots, which install-host.sh writes); an
# explicit SLOTS wins. 5 when neither is a number, at least 1, as in ci_supervisor.py.
SLOTS=${SLOTS:-$(cat /etc/homelab-ci/slots 2>/dev/null || true)}
SLOTS=${SLOTS//[[:space:]]/}
if [[ $SLOTS =~ ^[0-9]+$ ]]; then SLOTS=$((10#$SLOTS > 0 ? 10#$SLOTS : 1)); else SLOTS=5; fi
# Light slots (small VMs for light jobs, numbered after the build slots): LIGHT_SLOTS if
# given, else /etc/homelab-ci/light-slots, else 5, as in ci_supervisor.py; 0 is none.
LIGHT_SLOTS=${LIGHT_SLOTS:-$(cat /etc/homelab-ci/light-slots 2>/dev/null || true)}
LIGHT_SLOTS=${LIGHT_SLOTS//[[:space:]]/}
if [[ $LIGHT_SLOTS =~ ^[0-9]+$ ]]; then LIGHT_SLOTS=$((10#$LIGHT_SLOTS)); else LIGHT_SLOTS=5; fi
dir=$(cd "$(dirname "$0")" && pwd)
# The accept is limited to the uplink, so it opens nothing private even if the
# nftables table were missing.
uplink=$(ip -4 route show default | awk '{print $5; exit}')

accept_rules() {
  echo "-i fc-tap+ -o $uplink -j ACCEPT"
  echo "-i $uplink -o fc-tap+ -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"
}

refresh_wan() {
  local wan
  # A few tries: one slow answer should not cost a VM (run-vm.sh refuses to start without it).
  wan=$(curl -4 -sf -m 3 --retry 3 --retry-all-errors --retry-delay 1 https://1.1.1.1/cdn-cgi/trace | sed -n 's/^ip=//p')
  [[ "$wan" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "cannot find the public address" >&2; return 1; }
  # One transaction, so the set is never empty in between.
  nft -f - <<<"flush set inet homelab_ci wan4; add element inet homelab_ci wan4 { $wan }"
  nft list set inet homelab_ci wan4 | grep -qF "$wan"
}

case ${1:-up} in
  wan) refresh_wan; exit ;;
  up) ;;
  down)
    while read -r -a rule; do
      while iptables -D DOCKER-USER "${rule[@]}" 2>/dev/null; do :; done
    done < <(accept_rules)
    for tap in $(ip -br link | awk '$1 ~ /^fc-tap/ {print $1}'); do ip link del "$tap"; done
    nft delete table inet homelab_ci 2>/dev/null || true
    exit 0
    ;;
  *) echo "usage: setup-net.sh [up|down|wan]" >&2; exit 2 ;;
esac

[[ -n "$uplink" ]] || { echo "no default route" >&2; exit 1; }
nft -f "$dir/homelab-ci.nft"

for ((n = 0; n < SLOTS + LIGHT_SLOTS; n++)); do
  tap=fc-tap$n
  if ! ip link show "$tap" >/dev/null 2>&1; then
    ip tuntap add dev "$tap" mode tap user "fc-slot$n" group "fc-slot$n"
  fi
  sysctl -q -w "net.ipv6.conf.$tap.disable_ipv6=1"
  ip addr replace "10.200.$n.1/30" dev "$tap"
  ip link set "$tap" up
done
# Reloading the table emptied the public address set; VMs that start before it
# is filled again are refused by run-vm.sh, so a failure here is not fatal.
refresh_wan || echo "public address not set yet; run-vm.sh will retry" >&2

while read -r -a rule; do
  iptables -C DOCKER-USER "${rule[@]}" 2>/dev/null || iptables -I DOCKER-USER "${rule[@]}"
done < <(accept_rules)
