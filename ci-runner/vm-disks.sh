# shellcheck shell=bash
# A CI VM's disks without copying their images: each disk is a device-mapper
# snapshot that reads the image through a read-only loop device and puts the VM's
# writes in a sparse file. That file is deleted as soon as it is open and the loop
# devices detach themselves once the snapshot is removed, so whatever the VM wrote
# is gone with it, even if this script is killed or the host restarts.
# Sourced by run-vm.sh and stop-vms.sh; run as root.

cow_dir=/var/lib/homelab-ci/cow

# attach_disk SLOT NAME IMAGE NODE: snapshot IMAGE as /dev/mapper/ci-slotSLOT-NAME and
# make a device node for it at NODE (root's, 0600; the caller hands it over).
attach_disk() {
  local dm="ci-slot$1-$2" image=$3 node=$4 cow base cowdev sectors mm
  install -d -o root -g root -m 0700 "$cow_dir"
  cow=$(mktemp "$cow_dir/slot$1-$2.XXXXXX")
  # As big as the image, so it can hold every block the VM writes; sparse, so it only
  # takes what is written. The page cache keeps one copy of the image for all VMs.
  truncate -s $((($(stat -c %s "$image") + 511) / 512 * 512)) "$cow"
  base=$(losetup --find --show --read-only "$image")
  cowdev=$(losetup --find --show "$cow")
  rm -f "$cow"
  sectors=$(blockdev --getsz "$base")
  # Non-persistent (N), 4 KiB chunks: the ext4 block size.
  if ! dmsetup create "$dm" --table "0 $sectors snapshot $base $cowdev N 8"; then
    losetup -d "$base" "$cowdev"
    return 1
  fi
  # Held by the snapshot, so these only mark the loops to detach once it is removed.
  losetup -d "$base" "$cowdev"
  mm=$(dmsetup info -c --noheadings -o major,minor "$dm")
  mknod -m 0600 "$node" b "${mm%:*}" "${mm#*:}"
}

# detach_disks SLOT: remove the slot's snapshots, or every CI VM's when SLOT is "".
# Kill the VM first: an open snapshot cannot be removed.
detach_disks() {
  local prefix=ci-slot${1:+$1-} dm status=0
  for dm in $(dmsetup ls | awk -v p="$prefix" 'index($1, p) == 1 && $1 ~ /^ci-slot[0-9]+-/ {print $1}'); do
    dmsetup remove --retry "$dm" || status=1
  done
  # A write file left when a run died before deleting it.
  rm -f "$cow_dir/slot${1:+$1-}"*
  return $status
}
