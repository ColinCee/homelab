#!/usr/bin/env bash
# Prove the container wall (homelab-ci.nft, table homelab_wall):
#   sudo ci-runner/tests/test-wall.sh
# A throwaway container on its own Docker network must time out on Beelink's Tailscale ports,
# the router, the tailnet and the public address, yet reach the internet and its network peer.
# A stand-in LAN machine (a network namespace routing the Tailscale address through Beelink)
# must time out on the published ports. A target Beelink itself cannot reach is skipped, so a
# pass means the wall stopped it, not a closed port.
set -uo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
tailnet=$(ip -4 -br addr show tailscale0 | awk '{print $3}' | cut -d/ -f1)
router=$(ip -4 route show default | awk '{print $3; exit}')
wan=$(nft list set inet homelab_wall wan4 2>/dev/null | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' | head -n 1)
listening=$(ss -Htln "src $tailnet" | awk '{print $4}' | sed 's/%[^:]*//' | sort -u)
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

result() {
  if [[ $1 == "$2" ]]; then echo "PASS $3: $1"; else echo "FAIL $3: $1"; failures=$((failures + 1)); fi
}
host_reaches() { timeout 3 bash -c "exec 3<>/dev/tcp/${1%:*}/${1#*:}" 2>/dev/null; }
container_reaches() { docker run --rm --network "$net" "$image" nc -w 3 "${1%:*}" "${1#*:}" </dev/null >/dev/null 2>&1; }
from_container() {
  if container_reaches "$1"; then result open "$2" "container to $1"; else result blocked "$2" "container to $1"; fi
}

docker pull -q "$image" >/dev/null
docker network create "$net" >/dev/null
docker run -d --name homelab-wall-peer --network "$net" "$image" nc -lk -p 8080 -e cat >/dev/null

for target in $listening "$router:80" "$router:443" 100.100.100.100:53 ${wan:+"$wan:443"}; do
  if host_reaches "$target"; then from_container "$target" blocked; else echo "skip $target: Beelink cannot reach it"; fi
done
from_container 1.1.1.1:443 open
from_container homelab-wall-peer:8080 open

# Beelink sees the veth as it sees enp1s0: an interface that is not Docker's, a VM's or the tailnet.
ip netns add "$ns"
ip link add homelab-wall0 type veth peer name lan netns "$ns"
ip addr add 10.254.254.1/30 dev homelab-wall0
ip link set homelab-wall0 up
ip -n "$ns" addr add 10.254.254.2/30 dev lan
ip -n "$ns" link set lan up
ip -n "$ns" route add "$tailnet/32" via 10.254.254.1
for target in $published; do
  if ip netns exec "$ns" timeout 3 bash -c "exec 3<>/dev/tcp/${target%:*}/${target#*:}" 2>/dev/null; then
    result open blocked "LAN route to $target"
  else
    result blocked blocked "LAN route to $target"
  fi
done

if [[ $failures -eq 0 ]]; then echo "WALL TEST PASSED"; else echo "WALL TEST FAILED ($failures)"; exit 1; fi
