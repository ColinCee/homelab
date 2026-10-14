#!/usr/bin/env bash
# Bring up the CI microVM network: one tap per slot, the nftables rules, and the
# iptables accept Docker's FORWARD chain needs. Idempotent; run at boot by
# homelab-ci-net.service, after Docker.
set -euo pipefail

SLOTS=${SLOTS:-2}
dir=$(cd "$(dirname "$0")" && pwd)

nft -f "$dir/homelab-ci.nft"

for ((n = 0; n < SLOTS; n++)); do
  tap=fc-tap$n
  if ! ip link show "$tap" >/dev/null 2>&1; then
    ip tuntap add dev "$tap" mode tap user "fc-slot$n" group "fc-slot$n"
  fi
  ip addr replace "10.200.$n.1/30" dev "$tap"
  ip link set "$tap" up
done

# Docker's FORWARD policy is DROP and it leaves DOCKER-USER to us. Accept VM
# traffic here; the nftables table above has already dropped anything private.
rule() { iptables -C DOCKER-USER "$@" 2>/dev/null || iptables -I DOCKER-USER "$@"; }
rule -i fc-tap+ -j ACCEPT
rule -o fc-tap+ -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
