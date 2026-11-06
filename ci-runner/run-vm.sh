#!/usr/bin/env bash
# Boot one throwaway microVM in a slot, feed it a task through MMDS, wait for it
# to finish, and delete everything it wrote:
#   sudo run-vm.sh SLOT METADATA_JSON [TIMEOUT_SECONDS]
# The guest console is printed to stdout. The jailer runs Firecracker as the
# slot's own user, chrooted, with a syscall filter, under cgroup limits.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
slot=${1:?usage: run-vm.sh SLOT METADATA_JSON [TIMEOUT]}
metadata=${2:?usage: run-vm.sh SLOT METADATA_JSON [TIMEOUT]}
timeout=${3:-3600}
[[ "$slot" =~ ^[0-9]+$ ]] || { echo "bad slot" >&2; exit 2; }

VCPUS=${VCPUS:-2}
MEM_MIB=${MEM_MIB:-2048}
# 6 GiB for the tar (CACHE_MAX_UNPACKED_BYTES in ci_supervisor.py) and its header.
OUTBOX_BYTES=$((6 * 1024 * 1024 * 1024 + 1024 * 1024))
images=/var/lib/homelab-ci/images
base=/srv/jailer
id="ci-slot$slot"
user="fc-slot$slot"
uid=$(id -u "$user")
gid=$(id -g "$user")
chroot="$base/firecracker/$id/root"
cgroup="/sys/fs/cgroup/firecracker/$id"
# shellcheck source=ci-runner/vm-disks.sh
source "$(dirname "$0")/vm-disks.sh"

# One VM per slot at a time.
install -d -m 0700 /run/homelab-ci
exec 9>"/run/homelab-ci/slot$slot.lock"
flock -n 9 || { echo "slot $slot is busy" >&2; exit 75; }

# Block the home's current public address; refuse to start without it.
"$(dirname "$0")/net/setup-net.sh" wan || { echo "public address not blocked" >&2; exit 1; }

# Kill anything left in the slot's cgroup (say, after this script was killed)
# before deleting its files, so nothing of the slot user's survives.
cleanup() {
  if [[ -d "$cgroup" ]]; then
    echo 1 > "$cgroup/cgroup.kill" 2>/dev/null || true
    for _ in $(seq 1 50); do rmdir "$cgroup" 2>/dev/null && break; sleep 0.1; done
  fi
  if [[ -n ${sampler:-} ]]; then
    kill "$sampler" 2>/dev/null
    echo "slot $slot: VM ended ($status), its memory peaked at $(($(<"$peak_file") / 1048576)) MiB of $MEM_MIB" >&2
  fi
  rm -f "$peak_file"
  detach_disks "$slot" || true
  rm -rf "${base:?}/firecracker/$id"
}
peak_file=/run/homelab-ci/slot$slot.peak
cleanup
# The one-job registration goes with the VM (not at the start: it is this run's).
trap 'cleanup; rm -f "$metadata"' EXIT

# Fill the chroot as root, then hand it over, so the slot user cannot plant
# links for root to follow.
install -d -o root -g root -m 0700 "$chroot"
# The disks are snapshots of the images (vm-disks.sh), not copies: copying the 8 GB
# image took up to 10 minutes with ten VMs starting at once (5 Nov).
attach_disk "$slot" root "$images/rootfs.ext4" "$chroot/rootfs.ext4"
cp "$images/vmlinuz" "$chroot/vmlinuz"
install -m 0600 "$metadata" "$chroot/metadata.json"
# The repo's cache disk, if the supervisor names one: a snapshot, like the root
# disk, so the job can write to it and nothing it writes reaches the next job.
cache_drive=""
if [[ -n "${CACHE_IMAGE:-}" && -f "$CACHE_IMAGE" ]]; then
  attach_disk "$slot" cache "$CACHE_IMAGE" "$chroot/cache.ext4"
  cache_drive=', {"drive_id": "cache", "path_on_host": "cache.ext4", "is_root_device": false, "is_read_only": false}'
fi
# The outbox, if the supervisor names one: a raw disk, last in the VM's list, where a green
# default-branch job leaves its new cache (fc-cache action). It is not a snapshot: the file
# stays after the VM, for the supervisor to judge and turn into the next cache disk. Its
# first block says it is empty, so the action can tell it from any other disk.
outbox_drive=""
if [[ -n "${CACHE_OUT:-}" ]]; then
  install -o root -g root -m 0600 /dev/null "$CACHE_OUT"
  echo homelab-ci-outbox > "$CACHE_OUT"
  truncate -s "$OUTBOX_BYTES" "$CACHE_OUT"  # sparse: takes only what is written
  # A link, not a copy: the chroot and the outbox folder are on one filesystem. If not,
  # the VM goes without an outbox and its cache is simply not saved.
  if ln "$CACHE_OUT" "$chroot/outbox.img"; then
    outbox_drive=', {"drive_id": "outbox", "path_on_host": "outbox.img", "is_root_device": false, "is_read_only": false}'
  else
    echo "slot $slot: no outbox, $CACHE_OUT cannot be linked into the chroot" >&2
    rm -f "$CACHE_OUT"
  fi
fi

# The inbox, if the supervisor names one: a raw read-only disk, after the outbox, where the
# VM's console reader answers the job's handoff asks (handoff action). Its first block says
# it is empty, so the action can tell it from any other disk.
inbox_drive=""
if [[ -n "${INBOX:-}" ]]; then
  install -o root -g root -m 0600 /dev/null "$INBOX"
  echo homelab-ci-inbox-empty > "$INBOX"
  truncate -s "$OUTBOX_BYTES" "$INBOX"
  if ln "$INBOX" "$chroot/inbox.img"; then
    inbox_drive=', {"drive_id": "inbox", "path_on_host": "inbox.img", "is_root_device": false, "is_read_only": true}'
  else
    echo "slot $slot: no inbox, $INBOX cannot be linked into the chroot" >&2
    rm -f "$INBOX"
  fi
fi

# Extra guest kernel arguments, for experiments: root-only file, plain words only.
guest_args=""
if [[ -f /etc/homelab-ci/guest-args ]]; then
  extra=$(tr '\n' ' ' < /etc/homelab-ci/guest-args)
  [[ "$extra" =~ ^[a-z0-9=_,.\ -]*$ ]] || { echo "bad guest-args" >&2; exit 2; }
  guest_args=" ${extra% }"
fi

mac=$(printf '06:00:0a:c8:%02x:02' "$slot")
cat > "$chroot/vm.json" <<EOF
{
  "boot-source": {
    "kernel_image_path": "vmlinuz",
    "boot_args": "console=ttyS0 reboot=k panic=1 quiet loglevel=3 ci.ip=10.200.$slot.2/30 ci.gw=10.200.$slot.1$guest_args"
  },
  "drives": [
    {"drive_id": "rootfs", "path_on_host": "rootfs.ext4", "is_root_device": true, "is_read_only": false}$cache_drive$outbox_drive$inbox_drive
  ],
  "network-interfaces": [
    {"iface_id": "eth0", "guest_mac": "$mac", "host_dev_name": "fc-tap$slot"}
  ],
  "balloon": {"amount_mib": 0, "deflate_on_oom": false, "stats_polling_interval_s": 0, "free_page_reporting": true},
  "machine-config": {"vcpu_count": $VCPUS, "mem_size_mib": $MEM_MIB},
  "mmds-config": {"version": "V2", "network_interfaces": ["eth0"]}
}
EOF
chmod 0600 "$chroot/rootfs.ext4" "$chroot/vmlinuz" "$chroot/vm.json"
chown -h "$uid:$gid" "$chroot/rootfs.ext4" "$chroot/vmlinuz" "$chroot/metadata.json" "$chroot/vm.json" "$chroot"
if [[ -n "$cache_drive" ]]; then
  chmod 0600 "$chroot/cache.ext4"
  chown -h "$uid:$gid" "$chroot/cache.ext4"
fi
if [[ -n "$outbox_drive" ]]; then
  chown "$uid:$gid" "$chroot/outbox.img"
fi
if [[ -n "$inbox_drive" ]]; then
  chown "$uid:$gid" "$chroot/inbox.img"
fi

# Below the services in CPU priority. cpu.weight only ranks a cgroup against its
# siblings: the slot's own weight ranks the VMs against each other, the parent's
# ranks them all against system.slice, where the services run at 100.
mkdir -p "${cgroup%/*}"
echo 20 > "${cgroup%/*}/cpu.weight"
# The VM's largest use of its own memory (anon in its cgroup: not the host's file cache),
# sampled every 2 s and printed when it ends (cleanup), to size VMs by kind of job. It stops with this
# script, and holds neither the slot lock nor the console pipe.
echo 0 > "$peak_file"
(
  while sleep 2 && kill -0 $$ 2>/dev/null; do
    anon=$(awk '$1 == "anon" {print $2}' "$cgroup/memory.stat" 2>/dev/null) || continue
    [[ -n $anon ]] && ((anon > $(<"$peak_file"))) && echo "$anon" > "$peak_file"
  done
) </dev/null >/dev/null 2>&1 9>&- &
sampler=$!

# Memory capped a little above the guest's, with no swap: the cap also counts the host's cache of
# the VM's own disk, and at the cap the kernel swapped the guest's RAM out rather than drop that
# cache (6 Nov: VMs hit it thousands of times, up to 300 MiB each in swap). The guest's RAM fits
# under the cap, so now only that cache is let go.
# The lock stays with this script: if it dies, the next run kills the orphan.
status="stopped"
timeout --kill-after=10 "$timeout" /usr/local/bin/jailer \
  --id "$id" --exec-file /usr/local/bin/firecracker \
  --uid "$uid" --gid "$gid" --chroot-base-dir "$base" \
  --cgroup-version 2 --cgroup "cpu.weight=20" --cgroup "memory.max=$(((MEM_MIB + 256) * 1024 * 1024))" --cgroup "memory.swap.max=0" \
  -- --config-file vm.json --no-api --metadata metadata.json --level Warning </dev/null 9>&- && status="exit 0" || status="exit $?"
exit "${status#exit }"
