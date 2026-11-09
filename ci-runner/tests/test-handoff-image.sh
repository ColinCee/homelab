#!/usr/bin/env bash
# Prove a job in the VM image can still use its handoff disks:
#   ci-runner/tests/test-handoff-image.sh [IMAGE]
# Builds the image from ci-runner/rootfs unless given one, then, in a container of it as the
# runner user, runs the handoff action's prepare, a put and a get with sudo dropped, and
# ci-capacity, on loop devices at /dev/vdb and /dev/vdc read with direct reads, as in a VM
# (on tmpfs a direct read cannot fail). This side answers the asks on the console as the CI
# supervisor does. Needs Docker and sudo; CI runs it on pull requests that change either.
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
image=${1:-}
if [[ -z $image ]]; then
  image=homelab-ci-rootfs:handoff-test
  # Every pin in versions.env as a build argument, from this shell's environment.
  set -a
  # shellcheck source=/dev/null
  source "$repo/ci-runner/versions.env"
  set +a
  args=()
  while IFS='=' read -r name _; do
    [[ $name =~ ^[A-Z0-9_]+$ ]] && args+=(--build-arg "$name")
  done < "$repo/ci-runner/versions.env"
  docker build -q -t "$image" "${args[@]}" "$repo/ci-runner/rootfs" > /dev/null
fi

tmp=$(mktemp -d -p /var/tmp)
loops=()
answerer=""
cleanup() {
  [[ -n $answerer ]] && kill "$answerer" 2> /dev/null
  for loop in "${loops[@]}"; do sudo losetup -d "$loop"; done
  sudo rm -rf "$tmp"
}
trap cleanup EXIT

# The disks as run-vm.sh makes them; the inbox is read-only to the VM.
echo homelab-ci-outbox > "$tmp/outbox.img"
echo homelab-ci-inbox-empty > "$tmp/inbox.img"
truncate -s 4M "$tmp/outbox.img" "$tmp/inbox.img"
outbox=$(sudo losetup --find --show "$tmp/outbox.img")
loops+=("$outbox")
inbox=$(sudo losetup --find --show --read-only "$tmp/inbox.img")
loops+=("$inbox")
: > "$tmp/console"

# The supervisor's side (ci_supervisor.py, write_inbox): a get is given back the item the
# job put on its outbox, and ci-capacity some counts. Each ask overwrites the console file.
answer() {
  local answered="" line kind nonce
  while sleep 0.2; do
    while read -r line; do
      read -r kind _ nonce _ <<< "$line"
      [[ -n $nonce && " $answered " != *" $nonce "* ]] || continue
      answered+=" $nonce"
      case $kind in
        HOMELAB-HANDOFF)
          dd if="$tmp/outbox.img" of="$tmp/outer.tar" bs=512 skip=1 status=none
          tar -xOf "$tmp/outer.tar" ./builder.tar > "$tmp/body"
          ;;
        HOMELAB-CAPACITY) echo '{"machines": 1}' > "$tmp/body" ;;
        *) continue ;;
      esac
      dd if="$tmp/body" of="$tmp/inbox.img" bs=512 seek=1 conv=notrunc,fsync status=none
      printf 'homelab-ci-inbox v1 %s ok %s\n' "$nonce" "$(stat -c %s "$tmp/body")" |
        dd of="$tmp/inbox.img" conv=notrunc,fsync status=none
    done < "$tmp/console"
  done
}
answer &
answerer=$!

# shellcheck disable=SC2016 # expands in the container
job='set -euo pipefail
export RUNNER_TEMP=$(mktemp -d) GITHUB_RUN_ID=70 GITHUB_RUN_ATTEMPT=1
h=/handoff/handoff.sh
cd "$RUNNER_TEMP"
out=$(bash $h prepare 2>&1) && echo "$out"
[[ $out != *::warning::* ]] || { echo "FAIL prepare: $out"; exit 1; }
for disk in /dev/vdb /dev/vdc; do
  [[ $(stat -c %U $disk) == runner ]] || { echo "FAIL prepare left $disk to $(stat -c %U $disk)"; exit 1; }
done
export SUDO=false
mkdir items && echo "{\"verdict\": \"pass\"}" > items/verdict.json
bash $h put builder items review "" 1 | tee put.log
grep -q "handoff: put builder" put.log || { echo "FAIL put"; exit 1; }
bash $h get builder build 70 got 30 || { echo "FAIL get"; exit 1; }
cmp items/verdict.json got/verdict.json || { echo "FAIL get gave back something else"; exit 1; }
counts=$(CAPACITY_WAIT=30 ci-capacity) || { echo "FAIL ci-capacity"; exit 1; }
[[ $counts == "{\"machines\": 1}" ]] || { echo "FAIL ci-capacity printed $counts"; exit 1; }
echo PASS handoffs in the job image'
timeout 300 docker run --rm --user runner \
  --device "$outbox:/dev/vdb" --device "$inbox:/dev/vdc" \
  -v "$repo/.github/actions/handoff:/handoff:ro" -v "$tmp/console:/console" \
  -e HANDOFF_CONSOLE=/console -e CAPACITY_CONSOLE=/console \
  "$image" bash -c "$job"
