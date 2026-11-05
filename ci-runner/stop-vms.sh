#!/usr/bin/env bash
# Stop every CI VM and remove its files: to clear the host by hand. Restarting the
# supervisor leaves VMs running (each is a unit of its own) and takes them back.
set -uo pipefail

systemctl stop 'homelab-ci-vm-*' 2>/dev/null
for cgroup in /sys/fs/cgroup/firecracker/ci-slot*; do
  [[ -d "$cgroup" ]] && echo 1 > "$cgroup/cgroup.kill"
done
sleep 1
# shellcheck source=ci-runner/vm-disks.sh
source "$(dirname "$0")/vm-disks.sh"
detach_disks ""  # every slot
rm -f /run/homelab-ci/metadata-slot*.json /run/homelab-ci/vms/*
rm -rf /srv/jailer/firecracker/ci-slot*
exit 0
