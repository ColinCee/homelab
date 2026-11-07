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
# ROLE=pc on the PC's Hyper-V guest: ready runners registered through the Beelink's door
# (ci_supervisor.py pc), no GitHub token, no light slots. Kept in /etc/homelab-ci/role.
ROLE=${ROLE:-$(cat /etc/homelab-ci/role 2>/dev/null || echo beelink)}
[[ $ROLE == beelink || $ROLE == pc ]] || { echo "ROLE is beelink or pc" >&2; exit 2; }
service=homelab-ci-supervisor.service
if [[ $ROLE == pc ]]; then LIGHT_SLOTS=0 service=homelab-ci-pc.service; fi
# On the Beelink, the door for the PC's slots ("PC build slots" in ci-runner.md): at most
# PEER_SLOTS runners, else /etc/homelab-ci/peer-slots-max, else 0 (closed).
PEER_SLOTS=${PEER_SLOTS:-$(cat /etc/homelab-ci/peer-slots-max 2>/dev/null || true)}
PEER_SLOTS=${PEER_SLOTS//[[:space:]]/}
[[ $PEER_SLOTS =~ ^[0-9]+$ && $ROLE == beelink ]] && PEER_SLOTS=$((10#$PEER_SLOTS)) || PEER_SLOTS=0
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

# One unprivileged user per slot, light slots too; the jailer drops to it.
for ((n = 0; n < SLOTS + LIGHT_SLOTS; n++)); do
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
# The SSD's lifetime writes and wear, for Grafana (scripts/ssd_wear.py). The PC guest has no NVMe device of its own.
if [[ $ROLE == beelink ]]; then
  install -d -o root -g root -m 0755 "$dest"
  install -o root -g root -m 0644 "$here/../scripts/ssd_wear.py" "$dest/ssd_wear.py"
  for unit in homelab-ssd-wear.service homelab-ssd-wear.timer; do
    install -o root -g root -m 0644 "$here/systemd/$unit" "/etc/systemd/system/$unit"
  done
  systemctl daemon-reload
  systemctl enable --now homelab-ssd-wear.timer
fi

# The supervisor, the network rules and the slot counts take a restart. Running VMs carry
# on through it (each is a unit of its own, taken back by the new supervisor), and the
# rules reload in one transaction, so a restart takes seconds and cuts off nothing.
changed() { ! cmp -s "$1" "$2"; }
restart=false
# The door's user before any restart: the supervisor opens the door at start only if it exists.
if ((PEER_SLOTS > 0)) && [[ -f /etc/homelab-ci/peer.pub ]] && ! id ci-peer >/dev/null 2>&1; then
  useradd --system --user-group --home-dir / --shell /bin/sh ci-peer
  restart=true
fi
for pair in "../scripts/ci_supervisor.py:$dest/ci_supervisor.py" \
  "net/homelab-ci.nft:$dest/net/homelab-ci.nft" "net/setup-net.sh:$dest/net/setup-net.sh" \
  "systemd/homelab-ci-net.service:/etc/systemd/system/homelab-ci-net.service" \
  "systemd/$service:/etc/systemd/system/$service"; do
  if changed "$here/${pair%%:*}" "${pair#*:}"; then restart=true; fi
done
for setting in "slots:$SLOTS" "light-slots:$LIGHT_SLOTS" "role:$ROLE" "peer-slots-max:$PEER_SLOTS"; do
  [[ "$(cat "/etc/homelab-ci/${setting%%:*}" 2>/dev/null)" == "${setting#*:}" ]] || restart=true
done
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
  # (written by hand, see ci-runner.md). Which repos are served is the token's
  # repository access. Written before the network, which reads the slot count.
  install -d -o root -g root -m 0700 /etc/homelab-ci
  echo "$SLOTS" > /etc/homelab-ci/slots
  echo "$LIGHT_SLOTS" > /etc/homelab-ci/light-slots
  echo "$ROLE" > /etc/homelab-ci/role
  echo "$PEER_SLOTS" > /etc/homelab-ci/peer-slots-max

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
  if $was_running || [[ $ROLE == pc ]]; then
    systemctl enable --now "$service"
    how="network up, supervisor restarted"
  else
    how="network up, supervisor not running"
  fi
fi

# Each host takes main on its own (update.sh --follow, every minute): the Beelink from GitHub, the PC from the Beelink.
for unit in homelab-ci-update.service homelab-ci-update.timer; do
  install -o root -g root -m 0644 "$here/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now homelab-ci-update.timer

# The PC's door: user ci-peer may only reach the supervisor's socket, with the PC guest's
# key (/etc/homelab-ci/peer.pub) from the PC's address (/etc/homelab-ci/peer-from).
door_conf=/etc/ssh/sshd_config.d/60-ci-peer.conf door_keys=/etc/ssh/ci-peer_authorized_keys
# Its own port on the Tailscale address: Tailscale SSH answers port 22 there and would skip
# the forced command. sshd is socket-activated here, so the port is the socket's.
# Named to sort after every other drop-in: each starts with `ListenStream=`, which drops the
# ports set before it (Ubuntu's generator writes addresses.conf, and the Beelink has listen.conf).
door_socket=/etc/systemd/system/ssh.socket.d/zz-ci-peer.conf door_port=2222
if ((PEER_SLOTS > 0)) && [[ -f /etc/homelab-ci/peer.pub && -f /etc/homelab-ci/peer-from ]]; then
  command -v socat >/dev/null || apt-get install -y -qq socat >/dev/null
  from=$(tr -d '[:space:]' < /etc/homelab-ci/peer-from)
  [[ $from =~ ^[0-9.]+$ ]] || { echo "peer-from is not an address" >&2; exit 2; }
  # -t: after the PC's side closes, its reply (a GitHub call away) still comes back.
  socat_cmd="/usr/bin/socat -t 60 STDIO UNIX-CONNECT:/run/homelab-ci-peer/peer.sock"
  echo "from=\"$from\",restrict,command=\"$socat_cmd\" $(head -n 1 /etc/homelab-ci/peer.pub)" > "$door_keys.new"
  chmod 0644 "$door_keys.new"
  mv "$door_keys.new" "$door_keys"
  cat > "$door_conf.new" <<EOF
Match User ci-peer
    AuthorizedKeysFile $door_keys
    AuthenticationMethods publickey
    ForceCommand $socat_cmd
    AllowTcpForwarding no
    AllowAgentForwarding no
    AllowStreamLocalForwarding no
    X11Forwarding no
    PermitTTY no
    PermitTunnel no
    MaxSessions 1
    ClientAliveInterval 30
    ClientAliveCountMax 3
EOF
  mv "$door_conf.new" "$door_conf"
  # sshd -t needs this; socket activation makes it only when a connection comes.
  install -d -m 0755 /run/sshd
  address=$(tailscale ip -4 | head -n 1)
  [[ $address =~ ^[0-9.]+$ ]] || { echo "no Tailscale address for the door" >&2; exit 1; }
  printf '[Socket]\nListenStream=%s:%s\n' "$address" "$door_port" > "$door_socket.new"
  listening() { ss -Hltn "sport = :$door_port" | grep -q .; }
  # Also when the port is not up yet: a run cut off after writing the file never restarted it.
  if ! cmp -s "$door_socket.new" "$door_socket" || ! listening; then
    install -D -m 0644 "$door_socket.new" "$door_socket"
    sshd -t && systemctl daemon-reload && systemctl restart ssh.socket
  fi
  rm -f "$door_socket.new"
  listening || { echo "the door is not listening on $door_port" >&2; exit 1; }
  door="door open to $from on $address:$door_port"
else
  if [[ -f $door_conf || -f $door_socket ]]; then
    rm -f "$door_conf" "$door_keys" "$door_socket"
    systemctl daemon-reload && systemctl restart ssh.socket
  fi
  door="door closed"
fi

echo "Firecracker $(/usr/local/bin/firecracker --version | head -n 1 | awk '{print $2}'), $SLOTS slots and $LIGHT_SLOTS light slots ($ROLE), $how; $door."
