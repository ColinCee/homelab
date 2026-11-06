#!/usr/bin/env bash
# Usage: handoff.sh put NAME PATH READERS LATER DAYS
#        handoff.sh get NAME JOB RUN PATH WAIT
#        handoff.sh prepare
# Hands files between jobs through the Beelink, not GitHub artifacts (docs/runbooks/ci-runner.md,
# Handoffs). put writes every item this job has put so far onto the VM's outbox disk, which
# the CI supervisor files under this job's name once the VM ends. get asks for an item on
# the VM's console and reads the answer off its read-only inbox disk. prepare hands both
# disks and the console to the runner user, for a job that then drops sudo.
set -euo pipefail

sudo=${SUDO-sudo}
# The guest's page cache would hide what the host writes on the inbox.
direct=${HANDOFF_DIRECT-direct}
console=${HANDOFF_CONSOLE:-/dev/ttyS0}

# Runs a command on DISK directly if the runner may (MODE r or w), else through sudo.
as() {
  local disk=$1 mode=$2
  shift 2
  if [[ ($mode == r && -r $disk) || ($mode == w && -w $disk) ]]; then "$@"; else $sudo "$@"; fi
}

head_of() {
  as "$1" r dd if="$1" bs=512 count=1 ${direct:+iflag=$direct} status=none | head -n 1 | tr -d '\0'
}

# The disk (DISKS, default /dev/vd?) whose first line starts with MAGIC.
find_disk() {
  local disk
  for disk in ${DISKS:-/dev/vd?}; do
    [[ -e $disk ]] || continue
    [[ $(head_of "$disk") == "$1"* ]] && { echo "$disk"; return 0; }
  done
  return 1
}

put() {
  local name=$1 path=$2 readers=$3 later=$4 days=$5
  local stage=$RUNNER_TEMP/handoff-out outer=$RUNNER_TEMP/handoff-out.tar disk
  mkdir -p "$stage"
  if [[ -d $path && -n $(ls -A "$path") ]]; then
    tar -C "$path" -cf "$stage/$name.tar" .
  elif [[ -f $path ]]; then
    tar -C "$(dirname "$path")" -cf "$stage/$name.tar" "$(basename "$path")"
  else
    echo "handoff: nothing at $path, so no $name"
    return 0
  fi
  jq -n --arg r "$readers" --arg l "$later" --argjson d "$days" \
    '{readers: ($r | split(" ") | map(select(. != ""))), later: ($l | split(" ") | map(select(. != ""))), days: $d}' \
    > "$stage/$name.json"
  tar -C "$stage" -cf "$outer" .
  disk=$(find_disk homelab-ci-outbox || find_disk homelab-ci-handoff) || {
    echo "::warning::handoff: no outbox disk in this VM, $name not put"
    return 1
  }
  # The header goes first and last: a put cut short leaves no header over a half-written tar.
  echo homelab-ci-outbox | as "$disk" w dd of="$disk" conv=notrunc,fsync status=none
  as "$disk" w dd if="$outer" of="$disk" bs=1M seek=512 oflag=seek_bytes conv=notrunc,fsync status=none
  printf 'homelab-ci-handoff v1 run=%s attempt=%s\n' "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" |
    as "$disk" w dd of="$disk" conv=notrunc,fsync status=none
  rm -f "$outer"
  echo "handoff: put $name ($(stat -c %s "$stage/$name.tar") bytes)"
}

get() {
  local name=$1 job=$2 run=$3 path=$4 wait=$5 disk nonce line header deadline
  disk=$(find_disk homelab-ci-inbox) || {
    echo "::error::handoff: no inbox disk in this VM, so no $name"
    return 1
  }
  nonce=$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')
  line="HOMELAB-HANDOFF 1 $nonce $GITHUB_RUN_ID $GITHUB_RUN_ATTEMPT $run $job $name $wait"
  if [[ -w $console ]]; then echo "$line" > "$console"; else echo "$line" | $sudo tee "$console" > /dev/null; fi
  # The host gives up at WAIT; past it by two minutes, nothing is answering.
  deadline=$((SECONDS + wait + 120))
  until [[ $(head_of "$disk") =~ ^homelab-ci-inbox\ v1\ $nonce\ ([a-z]+)\ ([0-9]+)$ ]]; do
    ((SECONDS < deadline)) || { echo "::error::handoff: no answer about $name"; return 1; }
    sleep 1
  done
  local verdict=${BASH_REMATCH[1]} bytes=${BASH_REMATCH[2]}
  case $verdict in
    ok) ;;
    none) echo "::warning::handoff: no $name from $job in run $run"; return 1 ;;
    *) echo "::error::handoff: $name from $job in run $run is not for this job"; return 1 ;;
  esac
  mkdir -p "$path"
  # Whole 4 KiB blocks, as direct reads need; the tar's own end marks where it stops.
  as "$disk" r dd if="$disk" bs=1M iflag=skip_bytes,count_bytes${direct:+,$direct} \
    skip=512 count=$(((bytes + 4095) / 4096 * 4096)) status=none |
    head -c "$bytes" | tar -x -C "$path"
  echo "handoff: got $name from $job in run $run ($bytes bytes)"
}

prepare() {
  local disk magic
  for magic in homelab-ci-outbox homelab-ci-handoff homelab-ci-inbox; do
    if disk=$(find_disk "$magic"); then $sudo chown "$(id -un)" "$disk"; fi
  done
  $sudo chown "$(id -un)" "$console"
}

"$@"
