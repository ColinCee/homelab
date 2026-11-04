#!/usr/bin/env bash
# Check that a VM in every slot at full tilt leaves the services responsive:
#   sudo ci-runner/tests/test-load.sh [SECONDS]
# Boots a VM in every slot at the size the supervisor gives a job when others run
# (2 vCPUs, 2 GiB), each burning all its CPUs and holding nearly all its memory,
# while timing Home Assistant and Grafana once a second against a quiet baseline.
# Ends with one PASS or FAIL line; exits 1 when a VM does not boot or finish its
# burn, a request fails, or a service's worst response is over MAX_RESPONSE seconds.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
seconds=${1:-60}
if ! [[ $seconds =~ ^[0-9]+$ ]] || ((seconds <= 15)); then echo "SECONDS must be a number over 15" >&2; exit 2; fi
max_response=${MAX_RESPONSE:-1}
# The host's slot count (/etc/homelab-ci/slots, which install-host.sh writes); an
# explicit SLOTS wins. 3 when neither is a number, at least 1, as in ci_supervisor.py.
SLOTS=${SLOTS:-$(cat /etc/homelab-ci/slots 2>/dev/null || true)}
SLOTS=${SLOTS//[[:space:]]/}
if [[ $SLOTS =~ ^[0-9]+$ ]]; then SLOTS=$((10#$SLOTS > 0 ? 10#$SLOTS : 1)); else SLOTS=3; fi
# vm_size in ci_supervisor.py for a job started while others run.
export VCPUS=2 MEM_MIB=2048
run_vm=${RUN_VM:-/usr/local/lib/homelab-ci/run-vm.sh}
tailnet=$(ip -4 -br addr show tailscale0 | awk '{print $3}' | cut -d/ -f1)
names=("Home Assistant" "Grafana")
urls=("http://$tailnet:8123/" "http://$tailnet:3001/api/health")

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
reasons=()

mem_available() { awk '/^MemAvailable:/ {print $2}' /proc/meminfo; }
sample() { # PREFIX COUNT: one timing per service per second for COUNT seconds
  for _ in $(seq 1 "$2"); do
    for i in "${!urls[@]}"; do
      { curl -s -o /dev/null -m 10 -w '%{time_total} %{http_code}\n' "${urls[$i]}" || echo "fail 000"; } >> "$1.$i"
    done
    mem_available >> "$1.mem"
    sleep 1
  done
}
report() { # LABEL PREFIX: print each service's median and worst, and note what breaks the bounds
  for i in "${!urls[@]}"; do
    local line failed worst
    line=$(awk '$1 != "fail" && $2 >= 200 && $2 < 400 {print $1}' "$2.$i" | sort -n |
      awk '{a[NR]=$1} END {if (NR) printf "median %.3fs, worst %.3fs", a[int((NR+1)/2)], a[NR]}')
    failed=$(awk '$1 == "fail" || $2 < 200 || $2 >= 400' "$2.$i" | wc -l)
    echo "$1 ${names[$i]}: ${line:-no answers}, $failed failed"
    ((failed == 0)) || reasons+=("$1 ${names[$i]}: $failed requests failed")
    worst=$(awk '$1 != "fail" && $2 >= 200 && $2 < 400 {print $1}' "$2.$i" | sort -n | tail -1)
    if [[ -n $worst ]] && awk -v w="$worst" -v m="$max_response" 'BEGIN {exit !(w > m)}'; then
      reasons+=("$1 ${names[$i]}: worst ${worst}s over ${max_response}s")
    fi
  done
}

sample "$tmp/quiet" 15
report quiet "$tmp/quiet"

# Hold nearly all the VM's free memory in a tmpfs for the whole run, so the host
# carries every slot's full size at once, while every CPU spins.
burn="avail=\$(awk '/^MemAvailable:/ {print \$2}' /proc/meminfo)
burn_mib=\$((avail / 1024 - 128))
mkdir -p /run/burn && mount -t tmpfs -o size=\${burn_mib}M tmpfs /run/burn
head -c \${burn_mib}M /dev/zero > /run/burn/fill && echo \"BURN_MEM \${burn_mib} MiB\"
for i in \$(seq \$(nproc)); do timeout $seconds sh -c 'while :; do :; done' & done
wait
[ \"\$(stat -c %s /run/burn/fill)\" = \$((burn_mib * 1048576)) ] && echo BURN_DONE"
jq -n --arg probe "$burn" '{task: "probe", probe: $probe}' > "$tmp/metadata.json"
pids=()
for ((n = 0; n < SLOTS; n++)); do
  "$run_vm" "$n" "$tmp/metadata.json" $((seconds + 120)) > "$tmp/vm$n.log" 2>&1 &
  pids+=($!)
done
sleep 10
sample "$tmp/loaded" $((seconds - 15))
for ((n = 0; n < SLOTS; n++)); do
  status=0
  wait "${pids[$n]}" || status=$?
  if ! grep -q '=== probe start' "$tmp/vm$n.log"; then
    reasons+=("slot $n: VM did not boot (exit $status)")
    echo "slot $n: did not boot; its log:"; tail -20 "$tmp/vm$n.log"
  elif ! grep -q BURN_DONE "$tmp/vm$n.log"; then
    reasons+=("slot $n: VM did not finish its burn")
    echo "slot $n: did not finish its burn; its log:"; tail -20 "$tmp/vm$n.log"
  else
    echo "slot $n: held $(grep -o 'BURN_MEM [0-9]*' "$tmp/vm$n.log" | cut -d' ' -f2) MiB, burned and exited"
  fi
done
report loaded "$tmp/loaded"
echo "load average: $(cut -d' ' -f1-3 /proc/loadavg)"
echo "host lowest MemAvailable: $(sort -n "$tmp/loaded.mem" | head -1 | awk '{printf "%d MiB", $1 / 1024}')"

if ((${#reasons[@]} == 0)); then
  echo "PASS $SLOTS slots: every VM booted and finished, no request failed, worst response within ${max_response}s"
else
  reason=$(printf '%s; ' "${reasons[@]}")
  echo "FAIL $SLOTS slots: ${reason%; }"
  exit 1
fi
