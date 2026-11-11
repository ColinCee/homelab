#!/usr/bin/env bash
# Prove the container wall (homelab-ci.nft, table homelab_wall):
#   sudo ci-runner/tests/test-wall.sh
# A throwaway container on its own Docker network must be stopped by the wall on the ports
# Docker publishes on the Tailscale address, the router, the tailnet and the public address,
# yet reach the internet and its network peer, and a container on the default bridge must
# still resolve names. A stand-in LAN machine (a network namespace routing the Tailscale
# address through Beelink) must be stopped on the published ports. "Stopped" means the
# wall's drop counter rose, so a closed port or a broken probe cannot pass.
set -uo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
tailnet=$(ip -4 -br addr show tailscale0 | awk '{print $3}' | cut -d/ -f1)
router=$(ip -4 route show default | awk '{print $3; exit}')
wan=$(nft list set inet homelab_wall wan4 2>/dev/null | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' | head -n 1)
published=$(docker ps --format '{{.Ports}}' | grep -oE "$tailnet:[0-9]+->[0-9]+/tcp" | cut -d- -f1 | sort -u)
image=busybox:1.37
net=homelab-wall-test
ns=homelab-wall-lan
failures=0

cleanup() {
  docker rm -f homelab-wall-peer >/dev/null 2>&1
  docker network rm "$net" >/dev/null 2>&1
  ip netns del "$ns" 2>/dev/null
  ip link del homelab-wall0 2>/dev/null
}
trap cleanup EXIT
cleanup

pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; failures=$((failures + 1)); }
drops() { nft -j list chain inet homelab_wall "$1" | jq '[.. | .counter?.packets? // empty] | add // 0'; }
# Runs the probe in "$@"; passes when it fails and the chain's drop counter rose.
stopped() {
  local name=$1 chain=$2 before
  shift 2
  before=$(drops "$chain")
  if "$@"; then fail "$name: reached"
  elif (($(drops "$chain") > before)); then pass "$name: dropped by the wall"
  else fail "$name: failed, but not by the wall"
  fi
}
reaches() { local name=$1; shift; if "$@"; then pass "$name: reached"; else fail "$name: not reached"; fi; }
from_container() { docker run --rm --network "$net" "$image" nc -w 3 "${1%:*}" "${1#*:}" </dev/null >/dev/null 2>&1; }
from_lan() { ip netns exec "$ns" timeout 3 bash -c "exec 3<>/dev/tcp/${1%:*}/${1#*:}" 2>/dev/null; }

nft list table inet homelab_wall >/dev/null 2>&1 || { echo "FAIL the homelab_wall table is not loaded"; exit 1; }
[[ -n $published ]] || { echo "FAIL nothing is published on $tailnet to test against"; exit 1; }
docker pull -q "$image" >/dev/null
docker network create "$net" >/dev/null
docker run -d --name homelab-wall-peer --network "$net" "$image" nc -lk -p 8080 -e cat >/dev/null

for target in $published "$router:80" 100.100.100.100:53 ${wan:+"$wan:443"}; do
  stopped "container to $target" from_container from_container "$target"
done
reaches "container to the internet" from_container 1.1.1.1:443
reaches "container to its network peer" from_container homelab-wall-peer:8080
reaches "default-bridge container resolves names" docker run --rm "$image" nslookup example.com

# Beelink sees the veth as it sees enp1s0: an interface that is not Docker's, a VM's or the tailnet.
ip netns add "$ns"
ip link add homelab-wall0 type veth peer name lan netns "$ns"
ip addr add 10.254.254.1/30 dev homelab-wall0
ip link set homelab-wall0 up
ip -n "$ns" addr add 10.254.254.2/30 dev lan
ip -n "$ns" link set lan up
ip -n "$ns" route add "$tailnet/32" via 10.254.254.1
reaches "stand-in LAN machine reaches Beelink" ip netns exec "$ns" ping -c 1 -W 2 10.254.254.1
for target in $published; do
  stopped "LAN route to $target" forward from_lan "$target"
done

if ((failures == 0)); then echo "WALL TEST PASSED"; else echo "WALL TEST FAILED ($failures)"; exit 1; fi
