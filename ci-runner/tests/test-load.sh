#!/usr/bin/env bash
# Check that CI jobs at full tilt leave the services responsive:
#   sudo ci-runner/tests/test-load.sh [SECONDS]
# Boots a VM in every slot that burns all its CPUs and most of its memory, while
# timing Home Assistant and Grafana once a second. Compares with a quiet baseline.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
seconds=${1:-60}
slots=${SLOTS:-2}
run_vm=${RUN_VM:-/usr/local/lib/homelab-ci/run-vm.sh}
tailnet=$(ip -4 -br addr show tailscale0 | awk '{print $3}' | cut -d/ -f1)
urls=("http://$tailnet:8123/" "http://$tailnet:3001/api/health")

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

sample() { # FILE COUNT: worst and median response time over COUNT seconds
  for _ in $(seq 1 "$2"); do
    for url in "${urls[@]}"; do curl -s -o /dev/null -m 10 -w '%{time_total}\n' "$url"; done
    sleep 1
  done > "$1"
}
summary() { sort -n "$1" | awk '{a[NR]=$1} END {printf "median %.3fs, worst %.3fs\n", a[int((NR+1)/2)], a[NR]}'; }

sample "$tmp/quiet" 15
echo "quiet:   $(summary "$tmp/quiet")"

burn="for i in \$(seq \$(nproc)); do timeout $seconds sh -c 'while :; do :; done' & done
timeout $seconds sh -c 'head -c 1500M /dev/zero | tail > /dev/null' &
wait; echo BURN_DONE"
jq -n --arg probe "$burn" '{task: "probe", probe: $probe}' > "$tmp/metadata.json"
for ((n = 0; n < slots; n++)); do
  "$run_vm" "$n" "$tmp/metadata.json" $((seconds + 120)) > "$tmp/vm$n.log" 2>&1 &
done
sleep 10
sample "$tmp/loaded" $((seconds - 15))
wait
echo "loaded:  $(summary "$tmp/loaded")"
echo "load average: $(cut -d' ' -f1-3 /proc/loadavg)"
for ((n = 0; n < slots; n++)); do
  grep -q BURN_DONE "$tmp/vm$n.log" && echo "slot $n: burned and exited" || echo "slot $n: did not finish (see log)"
done
