#!/usr/bin/env bash
# Usage: save.sh TAR [DISK...]
# Writes TAR to the VM's outbox disk (the DISK, default /dev/vd?, whose first line is
# homelab-ci-outbox; see ci-runner/run-vm.sh), where the CI supervisor takes it as the
# repo's next cache disk once GitHub shows this job passed (ci_supervisor.py, take_outbox).
# The tar goes after the first 512 bytes and the header last, so a write cut short or
# refused (the tar too big for the disk) leaves no header and nothing is taken.
set -euo pipefail

tar=$1
shift
(($#)) || set -- /dev/vd?
sudo=${SUDO-sudo}

outbox=""
for disk in "$@"; do
  [[ -e $disk ]] && $sudo head -c 18 "$disk" | cmp -s - <(echo homelab-ci-outbox) && outbox=$disk
done
[[ -n $outbox ]] || { echo "::warning::fc-cache: no outbox disk in this VM" >&2; exit 1; }

$sudo dd if="$tar" of="$outbox" bs=1M seek=512 oflag=seek_bytes conv=notrunc,fsync status=none
printf 'homelab-ci-cache v1 run=%s attempt=%s\n' "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" |
  $sudo dd of="$outbox" conv=notrunc,fsync status=none
echo "fc-cache: saved to $outbox"
