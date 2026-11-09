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
# GNU dd where the image has it as gnudd: Ubuntu 26.04's own dd (uutils) reads into an
# unaligned buffer, so every direct read fails with "Invalid input".
dd=$(command -v gnudd || echo dd)

# Runs a command on DISK directly if the runner may (MODE r or w), else through sudo.
as() {
  local disk=$1 mode=$2
  shift 2
  if [[ ($mode == r && -r $disk) || ($mode == w && -w $disk) ]]; then "$@"; else $sudo "$@"; fi
}

head_of() {
  as "$1" r "$dd" if="$1" bs=512 count=1 ${direct:+iflag=$direct} status=none | head -n 1 | tr -d '\0'
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

# What the supervisor takes (ci_supervisor.py, HANDOFF_ITEM, HANDOFF_JOB, HANDOFF_MAX_WAIT_SECONDS).
item_name='^[a-z0-9][a-z0-9-]{0,63}$'
job_name='^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$'

put() {
  local name=$1 path=$2 readers=$3 later=$4 days=$5
  local stage=$RUNNER_TEMP/handoff-out outer=$RUNNER_TEMP/handoff-out.tar disk room
  [[ $name =~ $item_name ]] || { echo "::warning::handoff: $name is not an item name (a-z, 0-9, -)"; return 1; }
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
    echo "::warning::handoff: no outbox disk this job can read, $name not put"
    return 1
  }
  if [[ -b $disk ]]; then room=$(lsblk -bndo SIZE "$disk"); else room=$(stat -c %s "$disk"); fi
  if (($(stat -c %s "$outer") > room - 512)); then
    rm -f "$stage/$name.tar" "$stage/$name.json" "$outer"
    echo "::warning::handoff: $name does not fit on the outbox; the job's earlier items stay"
    return 1
  fi
  # The header goes first and last: a put cut short leaves no header over a half-written tar.
  echo homelab-ci-outbox | as "$disk" w "$dd" of="$disk" conv=notrunc,fsync status=none
  as "$disk" w "$dd" if="$outer" of="$disk" bs=1M seek=512 oflag=seek_bytes conv=notrunc,fsync status=none
  printf 'homelab-ci-handoff v1 run=%s attempt=%s\n' "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" |
    as "$disk" w "$dd" of="$disk" conv=notrunc,fsync status=none
  rm -f "$outer"
  echo "handoff: put $name ($(stat -c %s "$stage/$name.tar") bytes)"
}

get() {
  local name=$1 job=$2 run=$3 path=$4 wait=$5 disk nonce line deadline
  [[ $name =~ $item_name && $job =~ $job_name && $run =~ ^[0-9]{1,20}$ && $wait =~ ^[0-9]{1,4}$ ]] || {
    echo "::error::handoff: get needs an item name, a job name with no spaces, a run id and a wait in seconds"
    return 1
  }
  ((wait <= 1800)) || wait=1800
  disk=$(find_disk homelab-ci-inbox) || {
    echo "::error::handoff: no inbox disk this job can read, so no $name"
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
  as "$disk" r "$dd" if="$disk" bs=1M iflag=skip_bytes,count_bytes${direct:+,$direct} \
    skip=512 count=$(((bytes + 4095) / 4096 * 4096)) status=none |
    head -c "$bytes" | tar -x -C "$path"
  echo "handoff: got $name from $job in run $run ($bytes bytes)"
}

prepare() {
  local disk
  # Said here, not only at a later get: once the job drops sudo, a disk not handed over is lost.
  if disk=$(find_disk homelab-ci-outbox || find_disk homelab-ci-handoff); then
    $sudo chown "$(id -un)" "$disk"
  else
    echo "::warning::handoff: no outbox disk found to hand to the runner user"
  fi
  if disk=$(find_disk homelab-ci-inbox); then
    $sudo chown "$(id -un)" "$disk"
  else
    echo "::warning::handoff: no inbox disk found to hand to the runner user"
  fi
  $sudo chown "$(id -un)" "$console"
}

"$@"
