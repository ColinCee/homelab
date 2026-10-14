#!/usr/bin/env bash
# Bring up or tear down the CI microVM network: one tap per slot, the nftables
# rules, and the iptables accept Docker's FORWARD chain needs. Idempotent; run by
# homelab-ci-net.service after Docker.
#   setup-net.sh [up|down]
set -euo pipefail

SLOTS=${SLOTS:-2}
dir=$(cd "$(dirname "$0")" && pwd)
# The accept is limited to the uplink, so it opens nothing private even if the
# nftables table were missing.
uplink=$(ip -4 route show default | awk '{print $5; exit}')

accept_rules() {
  echo "-i fc-tap+ -o $uplink -j ACCEPT"
  echo "-i $uplink -o fc-tap+ -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"
}

if [[ ${1:-up} == down ]]; then
  while read -r -a rule; do
    while iptables -D DOCKER-USER "${rule[@]}" 2>/dev/null; do :; done
  done < <(accept_rules)
  for tap in $(ip -br link | awk '$1 ~ /^fc-tap/ {print $1}'); do ip link del "$tap"; done
  nft delete table inet homelab_ci 2>/dev/null || true
  exit 0
fi

[[ -n "$uplink" ]] || { echo "no default route" >&2; exit 1; }
nft -f "$dir/homelab-ci.nft"

for ((n = 0; n < SLOTS; n++)); do
  tap=fc-tap$n
  if ! ip link show "$tap" >/dev/null 2>&1; then
    ip tuntap add dev "$tap" mode tap user "fc-slot$n" group "fc-slot$n"
  fi
  sysctl -q -w "net.ipv4.conf.$tap.rp_filter=1"
  sysctl -q -w "net.ipv6.conf.$tap.disable_ipv6=1"
  ip addr replace "10.200.$n.1/30" dev "$tap"
  ip link set "$tap" up
done

while read -r -a rule; do
  iptables -C DOCKER-USER "${rule[@]}" 2>/dev/null || iptables -I DOCKER-USER "${rule[@]}"
done < <(accept_rules)
