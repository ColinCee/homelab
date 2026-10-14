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

cleanup() {
  rm -rf "${base:?}/firecracker/$id"
  rmdir "/sys/fs/cgroup/firecracker/$id" 2>/dev/null || true
}
cleanup
trap cleanup EXIT

install -d -o "$uid" -g "$gid" -m 0700 "$chroot"
cp --sparse=always "$images/rootfs.ext4" "$chroot/rootfs.ext4"
cp "$images/vmlinuz" "$chroot/vmlinuz"
install -o "$uid" -g "$gid" -m 0600 "$metadata" "$chroot/metadata.json"
chown "$uid:$gid" "$chroot/rootfs.ext4" "$chroot/vmlinuz"
chmod 0600 "$chroot/rootfs.ext4" "$chroot/vmlinuz"

mac=$(printf '06:00:0a:c8:%02x:02' "$slot")
cat > "$chroot/vm.json" <<EOF
{
  "boot-source": {
    "kernel_image_path": "vmlinuz",
    "boot_args": "console=ttyS0 reboot=k panic=1 quiet loglevel=3 ci.ip=10.200.$slot.2/30 ci.gw=10.200.$slot.1"
  },
  "drives": [
    {"drive_id": "rootfs", "path_on_host": "rootfs.ext4", "is_root_device": true, "is_read_only": false}
  ],
  "network-interfaces": [
    {"iface_id": "eth0", "guest_mac": "$mac", "host_dev_name": "fc-tap$slot"}
  ],
  "machine-config": {"vcpu_count": $VCPUS, "mem_size_mib": $MEM_MIB},
  "mmds-config": {"version": "V2", "network_interfaces": ["eth0"]}
}
EOF
chown "$uid:$gid" "$chroot/vm.json"

# Below the services in CPU priority; memory capped a little above the guest's.
timeout --kill-after=10 "$timeout" /usr/local/bin/jailer \
  --id "$id" --exec-file /usr/local/bin/firecracker \
  --uid "$uid" --gid "$gid" --chroot-base-dir "$base" \
  --cgroup-version 2 --cgroup "cpu.weight=20" --cgroup "memory.max=$(((MEM_MIB + 256) * 1024 * 1024))" \
  -- --config-file vm.json --no-api --metadata metadata.json --level Warning
