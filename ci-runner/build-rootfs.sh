#!/usr/bin/env bash
# Build the microVM root filesystem and copy Ubuntu's kernel next to it:
#   sudo ci-runner/build-rootfs.sh
# Writes /var/lib/homelab-ci/images/{rootfs.ext4,vmlinuz}, root-owned and
# read-only; each job boots a private copy. Rebuild after a kernel upgrade.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
images=/var/lib/homelab-ci/images
size=${ROOTFS_SIZE:-2G}

tmp=$(mktemp -d)
cid=""
cleanup() {
  [[ -n "$cid" ]] && docker rm -f "$cid" >/dev/null 2>&1
  rm -rf "$tmp"
}
trap cleanup EXIT

docker build -q -t homelab-ci-rootfs "$here/rootfs" >/dev/null
cid=$(docker create homelab-ci-rootfs)
mkdir "$tmp/root"
docker export "$cid" | tar -x -C "$tmp/root" --numeric-owner
# Docker bind-mounts these during the build, so set them here.
printf 'nameserver 1.1.1.1\nnameserver 9.9.9.9\n' > "$tmp/root/etc/resolv.conf"
echo ci-vm > "$tmp/root/etc/hostname"
rm -f "$tmp/root/.dockerenv"

install -d -o root -g root -m 0755 /var/lib/homelab-ci "$images"
truncate -s "$size" "$tmp/rootfs.ext4"
mkfs.ext4 -q -F -L rootfs -d "$tmp/root" "$tmp/rootfs.ext4"
install -o root -g root -m 0644 "$tmp/rootfs.ext4" "$images/rootfs.ext4"
install -o root -g root -m 0644 "/boot/vmlinuz-$(uname -r)" "$images/vmlinuz"

echo "rootfs $(du -h --apparent-size "$images/rootfs.ext4" | cut -f1), kernel $(uname -r)"
