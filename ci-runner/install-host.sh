#!/usr/bin/env bash
# Prepare Beelink to run CI jobs in Firecracker microVMs. Run from a checkout of
# main; deploys never touch these files, so a push cannot change them:
#   sudo ci-runner/install-host.sh
# Re-run after changing anything under ci-runner/ or versions.env.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=/dev/null
source "$here/versions.env"
# How many VMs run at once: SLOTS if given, else the count in /etc/homelab-ci/slots
# from the last run. 5 when neither is a number, at least 1, as in ci_supervisor.py.
# Written to that file below, so the supervisor, network and slot users all match:
#   sudo SLOTS=4 ci-runner/install-host.sh
SLOTS=${SLOTS:-$(cat /etc/homelab-ci/slots 2>/dev/null || true)}
SLOTS=${SLOTS//[[:space:]]/}
if [[ $SLOTS =~ ^[0-9]+$ ]]; then SLOTS=$((10#$SLOTS > 0 ? 10#$SLOTS : 1)); else SLOTS=5; fi
# Light slots (small VMs for light jobs, numbered after the build slots): LIGHT_SLOTS if
# given, else /etc/homelab-ci/light-slots, else 5, as in ci_supervisor.py; 0 is none.
LIGHT_SLOTS=${LIGHT_SLOTS:-$(cat /etc/homelab-ci/light-slots 2>/dev/null || true)}
LIGHT_SLOTS=${LIGHT_SLOTS//[[:space:]]/}
if [[ $LIGHT_SLOTS =~ ^[0-9]+$ ]]; then LIGHT_SLOTS=$((10#$LIGHT_SLOTS)); else LIGHT_SLOTS=5; fi
dest=/usr/local/lib/homelab-ci

# KSM merges identical memory pages across processes, which leaks data between
# VMs through timing. Firecracker's production guide says off.
echo 'w /sys/kernel/mm/ksm/run - - - - 0' > /etc/tmpfiles.d/homelab-ci-ksm.conf
echo 0 > /sys/kernel/mm/ksm/run

# Firecracker and jailer, pinned and checked against the hash in versions.env.
if ! /usr/local/bin/firecracker --version 2>/dev/null | grep -q "Firecracker ${FIRECRACKER_VERSION}\$"; then
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  tgz="firecracker-${FIRECRACKER_VERSION}-x86_64.tgz"
  curl -fsSL -o "$tmp/$tgz" \
    "https://github.com/firecracker-microvm/firecracker/releases/download/${FIRECRACKER_VERSION}/$tgz"
  echo "${FIRECRACKER_SHA256}  $tmp/$tgz" | sha256sum -c --quiet
  tar -xzf "$tmp/$tgz" -C "$tmp"
  rel="$tmp/release-${FIRECRACKER_VERSION}-x86_64"
  install -o root -g root -m 0755 "$rel/firecracker-${FIRECRACKER_VERSION}-x86_64" /usr/local/bin/firecracker
  install -o root -g root -m 0755 "$rel/jailer-${FIRECRACKER_VERSION}-x86_64" /usr/local/bin/jailer
fi

# One unprivileged user per slot, light slots too; the jailer drops to it.
for ((n = 0; n < SLOTS + LIGHT_SLOTS; n++)); do
  id "fc-slot$n" >/dev/null 2>&1 ||
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "fc-slot$n"
done

# Stop running VMs before touching the network: reloading the rules briefly
# empties the blocked public address set.
was_running=false
if systemctl is-active --quiet homelab-ci-supervisor.service; then
  echo "Stopping the supervisor; any running CI jobs will fail."
  was_running=true
  systemctl stop homelab-ci-supervisor.service
fi
if pgrep -x firecracker >/dev/null; then
  echo "a VM is still running (started by hand?); wait for it to finish" >&2
  exit 1
fi

# Host-side settings, never in the repo: the slot count, and the GitHub token
# (written by hand, see ci-runner.md). Which repos are served is the token's
# repository access. Written before the network, which reads the slot count.
install -d -o root -g root -m 0700 /etc/homelab-ci
echo "$SLOTS" > /etc/homelab-ci/slots
echo "$LIGHT_SLOTS" > /etc/homelab-ci/light-slots

install -d -o root -g root -m 0755 "$dest" "$dest/net"
install -o root -g root -m 0644 "$here/net/homelab-ci.nft" "$dest/net/homelab-ci.nft"
install -o root -g root -m 0755 "$here/net/setup-net.sh" "$dest/net/setup-net.sh"
install -o root -g root -m 0755 "$here/run-vm.sh" "$dest/run-vm.sh"
install -o root -g root -m 0755 "$here/stop-vms.sh" "$dest/stop-vms.sh"
install -o root -g root -m 0644 "$here/vm-disks.sh" "$dest/vm-disks.sh"
install -o root -g root -m 0755 "$here/../scripts/ci_supervisor.py" "$dest/ci_supervisor.py"
install -o root -g root -m 0644 "$here/systemd/homelab-ci-net.service" /etc/systemd/system/homelab-ci-net.service
install -o root -g root -m 0644 "$here/systemd/homelab-ci-supervisor.service" /etc/systemd/system/homelab-ci-supervisor.service
systemctl daemon-reload
systemctl enable --now homelab-ci-net.service
# Apply rule changes in place; restarting the unit would remove the taps.
"$dest/net/setup-net.sh" up

# Cache disks the supervisor builds from main-only artifacts; VMs read them, never write.
install -d -o root -g root -m 0755 /var/lib/homelab-ci/caches
if $was_running; then
  systemctl start homelab-ci-supervisor.service
fi

echo "Firecracker $(/usr/local/bin/firecracker --version | head -n 1 | awk '{print $2}'), $SLOTS slots and $LIGHT_SLOTS light slots, network up."
