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
SLOTS=${SLOTS:-2}
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

# One unprivileged user per slot; the jailer drops to it.
for ((n = 0; n < SLOTS; n++)); do
  id "fc-slot$n" >/dev/null 2>&1 ||
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "fc-slot$n"
done

install -d -o root -g root -m 0755 "$dest" "$dest/net"
install -o root -g root -m 0644 "$here/net/homelab-ci.nft" "$dest/net/homelab-ci.nft"
install -o root -g root -m 0755 "$here/net/setup-net.sh" "$dest/net/setup-net.sh"
install -o root -g root -m 0755 "$here/run-vm.sh" "$dest/run-vm.sh"
install -o root -g root -m 0755 "$here/../scripts/ci_supervisor.py" "$dest/ci_supervisor.py"
install -o root -g root -m 0644 "$here/systemd/homelab-ci-net.service" /etc/systemd/system/homelab-ci-net.service
install -o root -g root -m 0644 "$here/systemd/homelab-ci-supervisor.service" /etc/systemd/system/homelab-ci-supervisor.service
systemctl daemon-reload
systemctl enable --now homelab-ci-net.service
# Apply rule changes in place; restarting the unit would remove the taps.
"$dest/net/setup-net.sh" up

# Host-side settings, never in the repo: which private repos to serve, and the
# GitHub token (written by hand, see ci-runner.md).
install -d -o root -g root -m 0700 /etc/homelab-ci
[[ -f /etc/homelab-ci/repos ]] || install -o root -g root -m 0600 /dev/null /etc/homelab-ci/repos
if systemctl is-active --quiet homelab-ci-supervisor.service; then
  echo "Restarting the supervisor; any running CI jobs will fail."
  systemctl restart homelab-ci-supervisor.service
fi

echo "Firecracker $(/usr/local/bin/firecracker --version | head -n 1 | awk '{print $2}'), $SLOTS slots, network up."
