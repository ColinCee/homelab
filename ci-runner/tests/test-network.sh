#!/usr/bin/env bash
# Prove the microVM network rules from inside a real VM:
#   sudo ci-runner/tests/test-network.sh
# Boots a VM in slot 0 running network-probe.sh, and while it listens tries to
# connect to it from a Docker container. Exits non-zero on any failure.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
run_vm=${RUN_VM:-/usr/local/lib/homelab-ci/run-vm.sh}

host_lan=$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')
tailnet=$(ip -4 -br addr show tailscale0 | awk '{print $3}' | cut -d/ -f1)

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
sed -e "s/@HOST_LAN@/$host_lan/g" -e "s/@TAILNET@/$tailnet/g" -e "s/@OTHER_SLOT@/10.200.1.2/g" \
  "$here/network-probe.sh" > "$tmp/probe.sh"
jq -n --rawfile probe "$tmp/probe.sh" '{task: "probe", probe: $probe}' > "$tmp/metadata.json"

"$run_vm" 0 "$tmp/metadata.json" 180 > "$tmp/console.log" 2>&1 &
vm=$!

# Inbound from a container (a forwarded path, like any other host on the network).
# The drop counter must rise, so we know our rule stopped it, not a missing listener.
inbound_drops() { nft -j list chain inet homelab_ci forward |
  jq '[.nftables[].rule? | select(.expr? and (.expr | tostring | test("oifname"))) | .expr[] | .counter?.packets // empty] | last'; }
docker pull -q busybox:1.37 >/dev/null
for _ in $(seq 1 120); do grep -q LISTENING "$tmp/console.log" && break; sleep 1; done
before=$(inbound_drops)
inbound=blocked
if docker run --rm --network bridge busybox:1.37 nc -w 3 10.200.0.2 8080 </dev/null >/dev/null 2>&1; then
  inbound=reached
fi
[[ $(inbound_drops) -gt $before ]] || inbound="not dropped by our rule"
wait "$vm" || true

results=$(grep -oE '(PASS|FAIL) .*' "$tmp/console.log" || true)
echo "$results"
echo "inbound from container: $inbound"
grep -q INBOUND_CONNECTED "$tmp/console.log" && inbound=reached

passes=$(grep -c '^PASS' <<<"$results" || true)
fails=$(grep -c '^FAIL' <<<"$results" || true)
if [[ $passes -lt 20 || $fails -gt 0 || $inbound != blocked ]]; then
  echo "NETWORK TEST FAILED ($passes passed, $fails failed, inbound $inbound)"
  tail -n 30 "$tmp/console.log"
  exit 1
fi
echo "network test passed ($passes checks, inbound blocked)"
