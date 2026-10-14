#!/usr/bin/env bash
# Kill every CI VM and remove its one-job registration file. Run when the
# supervisor stops: the jailer moves Firecracker into its own cgroup, outside
# the supervisor's, so stopping the service alone would not reach it.
set -uo pipefail

for cgroup in /sys/fs/cgroup/firecracker/ci-slot*; do
  [[ -d "$cgroup" ]] && echo 1 > "$cgroup/cgroup.kill"
done
rm -f /run/homelab-ci/metadata-slot*.json
exit 0
