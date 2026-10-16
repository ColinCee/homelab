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
images=/var/lib/homelab-ci/images
base=/srv/jailer
id="ci-slot$slot"
user="fc-slot$slot"
uid=$(id -u "$user")
gid=$(id -g "$user")
chroot="$base/firecracker/$id/root"
cgroup="/sys/fs/cgroup/firecracker/$id"

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
  rm -rf "${base:?}/firecracker/$id"
}
cleanup
trap cleanup EXIT

# Fill the chroot as root, then hand it over, so the slot user cannot plant
# links for root to follow.
install -d -o root -g root -m 0700 "$chroot"
cp --sparse=always "$images/rootfs.ext4" "$chroot/rootfs.ext4"
cp "$images/vmlinuz" "$chroot/vmlinuz"
install -m 0600 "$metadata" "$chroot/metadata.json"
# The repo's cache disk, if the supervisor names one: a private copy, like the root
# disk, so the job can write to it and nothing it writes reaches the next job.
cache_drive=""
if [[ -n "${CACHE_IMAGE:-}" && -f "$CACHE_IMAGE" ]]; then
  cp --sparse=always "$CACHE_IMAGE" "$chroot/cache.ext4"
  cache_drive=', {"drive_id": "cache", "path_on_host": "cache.ext4", "is_root_device": false, "is_read_only": false}'
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
    {"drive_id": "rootfs", "path_on_host": "rootfs.ext4", "is_root_device": true, "is_read_only": false}$cache_drive
  ],
  "network-interfaces": [
    {"iface_id": "eth0", "guest_mac": "$mac", "host_dev_name": "fc-tap$slot"}
  ],
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

# Below the services in CPU priority; memory capped a little above the guest's.
# The lock stays with this script: if it dies, the next run kills the orphan.
timeout --kill-after=10 "$timeout" /usr/local/bin/jailer \
  --id "$id" --exec-file /usr/local/bin/firecracker \
  --uid "$uid" --gid "$gid" --chroot-base-dir "$base" \
  --cgroup-version 2 --cgroup "cpu.weight=20" --cgroup "memory.max=$(((MEM_MIB + 256) * 1024 * 1024))" \
  -- --config-file vm.json --no-api --metadata metadata.json --level Warning </dev/null 9>&-
