#!/usr/bin/env bash
# Prepare Beelink to run CI jobs in Firecracker microVMs. Run from a checkout of
# main; deploys never touch these files:
#   sudo ci-runner/install-host.sh
# update.sh reruns it when main changes anything under ci-runner/ or scripts/.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=/dev/null
source "$here/versions.env"
# The slot count (slots.sh), written to /etc/homelab-ci below so the supervisor, network and slot users match:
#   sudo SLOTS=4 ci-runner/install-host.sh
# shellcheck source=ci-runner/slots.sh
source "$here/slots.sh"
service=homelab-ci-supervisor.service
dest=/usr/local/lib/homelab-ci

# KSM merges identical memory pages across processes, which leaks data between
# VMs through timing. Firecracker's production guide says off.
echo 'w /sys/kernel/mm/ksm/run - - - - 0' > /etc/tmpfiles.d/homelab-ci-ksm.conf
echo 0 > /sys/kernel/mm/ksm/run

# Firecracker and jailer, pinned and checked against the hash in versions.env.
if ! /usr/local/bin/firecracker --version 2>/dev/null | grep -q "Firecracker ${FIRECRACKER_VERSION}\$" ||
  ! /usr/local/bin/jailer --version 2>/dev/null | grep -q "Jailer ${FIRECRACKER_VERSION}\$"; then
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

# One install at a time: a second waits its turn and is told whose is running.
install -d -o root -g root -m 0700 /run/homelab-ci
exec 8>/run/homelab-ci/install.lock
if ! flock -n 8; then
  echo "Another install is running ($(cat /run/homelab-ci/restart-reason 2>/dev/null || echo "reason unknown")); waiting for it."
  flock 8
fi
echo "${REASON:-${SUDO_USER:-root}: install-host.sh}" > /run/homelab-ci/restart-reason
was_running=false
# However this ends, the reason goes and a supervisor that was running runs again.
finish() {
  rm -f /run/homelab-ci/restart-reason
  if $was_running && ! systemctl is-active --quiet "$service"; then
    systemctl start "$service" || true
  fi
}
trap finish EXIT

# Files a VM reads when it starts. Without a restart they are swapped while jobs run: new VMs get them,
# running ones keep the copies they opened (install writes a new file, never the old one in place).
install_vm_files() {
  install -d -o root -g root -m 0755 "$dest" "$dest/net"
  install -o root -g root -m 0644 "$here/vm-disks.sh" "$dest/vm-disks.sh"
  install -o root -g root -m 0755 "$here/run-vm.sh" "$dest/run-vm.sh"
  install -o root -g root -m 0755 "$here/stop-vms.sh" "$dest/stop-vms.sh"
  install -o root -g root -m 0755 "$here/update.sh" "$dest/update.sh"
  install -o root -g root -m 0755 "$here/tests/test-load.sh" "$dest/test-load.sh"
}
# Cache disks the supervisor builds from main-only outboxes; VMs read them, never write.
# Outboxes are hard-linked into the jails (run-vm.sh), so they share their filesystem.
install -d -o root -g root -m 0755 /var/lib/homelab-ci/caches
install -d -o root -g root -m 0700 /var/lib/homelab-ci/outbox
# The SSD is trimmed hourly (the reason is in the file). Restarted only when it changes, as a restart
# can run a trim missed under the weekly schedule at once.
trim=/etc/systemd/system/fstrim.timer.d/hourly.conf
if ! cmp -s "$here/systemd/fstrim-hourly.conf" "$trim"; then
  install -D -o root -g root -m 0644 "$here/systemd/fstrim-hourly.conf" "$trim"
  systemctl daemon-reload
  systemctl restart fstrim.timer
fi
# The SSD's lifetime writes and wear, for Prometheus (scripts/ssd_wear.py).
# Alloy's textfile metrics. Root owns it, as the backup container's root has no right to write into another user's
# folder (cap_drop ALL); sticky and open to all, as Colin's user timers (PC backups) write here too.
install -d -o root -g root -m 1777 /var/lib/homelab-metrics
install -d -o root -g root -m 0755 "$dest"
install -o root -g root -m 0644 "$here/../scripts/ssd_wear.py" "$dest/ssd_wear.py"
for unit in homelab-ssd-wear.service homelab-ssd-wear.timer; do
  install -o root -g root -m 0644 "$here/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now homelab-ssd-wear.timer
# What the intrusion alerts read (scripts/security_facts.py), as a user of its own in the journal's group.
id homelab-facts >/dev/null 2>&1 ||
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin homelab-facts
install -o root -g root -m 0644 "$here/../scripts/security_facts.py" "$dest/security_facts.py"
for unit in homelab-security-facts.service homelab-security-facts.timer; do
  install -o root -g root -m 0644 "$here/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now homelab-security-facts.timer
# CI and deploy run times, for the trace store and Prometheus (scripts/ci_times.py).
install -o root -g root -m 0644 "$here/../scripts/ci_times.py" "$dest/ci_times.py"
for unit in homelab-ci-times.service homelab-ci-times.timer; do
  install -o root -g root -m 0644 "$here/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now homelab-ci-times.timer

# The supervisor, the network rules and the slot count take a restart. Running VMs carry
# on through it (each is a unit of its own, taken back by the new supervisor), and the
# rules reload in one transaction, so a restart takes seconds and cuts off nothing.
changed() { ! cmp -s "$1" "$2"; }
restart=false
for pair in "../scripts/ci_supervisor.py:$dest/ci_supervisor.py" \
  "net/homelab-ci.nft:$dest/net/homelab-ci.nft" "net/setup-net.sh:$dest/net/setup-net.sh" \
  "systemd/homelab-ci-net.service:/etc/systemd/system/homelab-ci-net.service" \
  "systemd/$service:/etc/systemd/system/$service"; do
  if changed "$here/${pair%%:*}" "${pair#*:}"; then restart=true; fi
done
[[ "$(cat /etc/homelab-ci/slots 2>/dev/null)" == "$SLOTS" ]] || restart=true
systemctl is-active --quiet "$service" || restart=true

if ! $restart; then
  install_vm_files
  how="installed while jobs ran, no restart needed"
else
  if systemctl is-active --quiet "$service"; then
    was_running=true
    # A supervisor from before VMs ran as their own units owns its VMs: stopping it waits
    # for their jobs (up to 20 minutes) instead of cutting them off.
    if ! grep -q UNIT_PREFIX "$dest/ci_supervisor.py"; then
      echo "The running supervisor owns its VMs: waiting for their jobs to finish (up to 20 minutes)."
    fi
    systemctl stop "$service"
  fi

  # Host-side settings, never in the repo: the slot count, and the GitHub token
  # (written by hand, see docs/architecture.md, "CI runner"). Which repos are served is the token's
  # repository access. Written before the network, which reads the slot count.
  install -d -o root -g root -m 0700 /etc/homelab-ci
  echo "$SLOTS" > /etc/homelab-ci/slots

  install_vm_files
  install -o root -g root -m 0644 "$here/net/homelab-ci.nft" "$dest/net/homelab-ci.nft"
  install -o root -g root -m 0755 "$here/net/setup-net.sh" "$dest/net/setup-net.sh"
  install -o root -g root -m 0755 "$here/../scripts/ci_supervisor.py" "$dest/ci_supervisor.py"
  install -o root -g root -m 0644 "$here/systemd/homelab-ci-net.service" /etc/systemd/system/homelab-ci-net.service
  install -o root -g root -m 0644 "$here/systemd/$service" "/etc/systemd/system/$service"
  systemctl daemon-reload
  systemctl enable --now homelab-ci-net.service
  # Apply rule changes in place; restarting the unit would remove the taps.
  "$dest/net/setup-net.sh" up
  if $was_running; then
    systemctl enable --now "$service"
    how="network up, supervisor restarted"
  else
    how="network up, supervisor not running"
  fi
fi

# The Beelink takes main on its own (update.sh --follow, every minute).
for unit in homelab-ci-update.service homelab-ci-update.timer; do
  install -o root -g root -m 0644 "$here/systemd/$unit" "/etc/systemd/system/$unit"
done
# The Beelink's load test, which update.sh starts when its slot count has no result (docs/architecture.md, "CI runner").
install -o root -g root -m 0644 "$here/systemd/homelab-ci-load-test.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now homelab-ci-update.timer

echo "Firecracker $(/usr/local/bin/firecracker --version | head -n 1 | awk '{print $2}'), $SLOTS slots, $how."
