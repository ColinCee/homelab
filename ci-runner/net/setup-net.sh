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
dir=$(cd "$(dirname "$0")" && pwd)
# The accept is limited to the uplink, so it opens nothing private even if the
# nftables table were missing.
uplink=$(ip -4 route show default | awk '{print $5; exit}')

accept_rules() {
  echo "-i fc-tap+ -o $uplink -j ACCEPT"
  echo "-i $uplink -o fc-tap+ -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"
}

public_address() {
  local wan
  # A few tries: one slow answer should not cost a VM (run-vm.sh refuses to start without it).
  wan=$(curl -4 -sf -m 3 --retry 3 --retry-all-errors --retry-delay 1 https://1.1.1.1/cdn-cgi/trace | sed -n 's/^ip=//p')
  [[ "$wan" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "cannot find the public address" >&2; return 1; }
  echo "$wan"
}

refresh_wan() {
  local wan
  wan=$(public_address) || return 1
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
# The rules and the public address in one transaction, so running VMs never see the blocked
# address set empty. Without the address, rules already in place are kept; on a first start
# they load without it, and run-vm.sh refuses VMs until it is set.
# Every fc-slot user that exists, not the slot counts, so a user left from a larger
# count stays walled off too.
uids=$(getent passwd | awk -F: '$1 ~ /^fc-slot[0-9]+$/ {print $3}' | paste -sd, -)
slot_uids=${uids:+"add element inet homelab_ci slot_uids { $uids }"}
if wan=$(public_address); then
  { cat "$dir/homelab-ci.nft"; echo "add element inet homelab_ci wan4 { $wan }"; echo "$slot_uids"; } | nft -f -
elif nft list table inet homelab_ci >/dev/null 2>&1; then
  echo "public address not found: the current rules stay; run-vm.sh refreshes it before each VM" >&2
  # The kept table may be from before the slot-user wall.
  nft list set inet homelab_ci slot_uids >/dev/null 2>&1 || nft add set inet homelab_ci slot_uids '{ type uid; }'
  nft list chain inet homelab_ci output | grep -q 'skuid @slot_uids' ||
    nft insert rule inet homelab_ci output meta skuid @slot_uids counter drop
  echo "$slot_uids" | nft -f -
else
  { cat "$dir/homelab-ci.nft"; echo "$slot_uids"; } | nft -f -
fi

for ((n = 0; n < SLOTS; n++)); do
  tap=fc-tap$n
  if ! ip link show "$tap" >/dev/null 2>&1; then
    ip tuntap add dev "$tap" mode tap user "fc-slot$n" group "fc-slot$n"
  fi
  sysctl -q -w "net.ipv6.conf.$tap.disable_ipv6=1"
  ip addr replace "10.200.$n.1/30" dev "$tap"
  ip link set "$tap" up
done

while read -r -a rule; do
  iptables -C DOCKER-USER "${rule[@]}" 2>/dev/null || iptables -I DOCKER-USER "${rule[@]}"
done < <(accept_rules)
