#!/usr/bin/env bash
# Prove the image's tools run for a job, after building the image:
#   sudo ci-runner/tests/test-tools.sh
# Boots a VM in slot 0 and runs each tool as the runner user, with the PATH
# ci-boot gives the runner. Exits non-zero if any fails.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
run_vm=${RUN_VM:-/usr/local/lib/homelab-ci/run-vm.sh}

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# shellcheck disable=SC2016 # expands inside the VM, not here
probe='tools=("node --version" "uv --version" "java -version" "mise --version" "bun --version"
  "pnpm --version" "docker compose version")
for t in "${tools[@]}"; do
  if out=$(cd /home/runner && sudo -u runner -H env "PATH=$(cat /opt/runner/.path)" bash -c "$t" 2>&1); then
    echo "PASS $t: $(head -n 1 <<<"$out")"
  else
    echo "FAIL $t: $(tail -n 1 <<<"$out")"
  fi
done
echo "TOTAL ${#tools[@]}"'
jq -n --arg probe "$probe" '{task: "probe", probe: $probe}' > "$tmp/metadata.json"
"$run_vm" 0 "$tmp/metadata.json" 90 > "$tmp/console.log" 2>&1 || true

results=$(grep -oE '(PASS|FAIL) .*' "$tmp/console.log" | tr -d '\r' || true)
echo "$results"
passes=$(grep -c '^PASS' <<<"$results" || true)
total=$(grep -oE 'TOTAL [0-9]+' "$tmp/console.log" | awk '{print $2}')
if [[ -z "$total" || $passes -ne $total ]]; then
  echo "TOOLS TEST FAILED ($passes of ${total:-?} passed)"
  tail -n 30 "$tmp/console.log"
  exit 1
fi
echo "tools test passed ($passes tools)"
