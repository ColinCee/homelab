#!/usr/bin/env bash
# Blast-radius audit for the CI runner. Must pass before the supervisor runs,
# every quarter, and after any Firecracker, kernel or runner upgrade:
#   sudo ci-runner/tests/audit.sh
# Checks what a job could reach if it broke out of its VM, or out of
# Firecracker into the slot user. Read-only apart from one throwaway VM.
set -uo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
# Its live-VM checks must inspect its own VM, not one running a job.
if systemctl is-active --quiet homelab-ci-supervisor.service || pgrep -x firecracker >/dev/null; then
  echo "stop the supervisor and wait for running VMs first" >&2
  exit 1
fi
here=$(cd "$(dirname "$0")" && pwd)
run_vm=${RUN_VM:-/usr/local/lib/homelab-ci/run-vm.sh}
lib=/usr/local/lib/homelab-ci
images=/var/lib/homelab-ci/images
failures=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; failures=$((failures + 1)); }
check() { local name=$1; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
mode_is() { [[ $(stat -c '%U:%G %a' "$1" 2>/dev/null) == "$2" ]]; }
# Root-owned and writable by root alone.
root_only_writes() { [[ $(stat -c '%U:%G' "$1") == root:root ]] && ! find "$1" -perm /022 | grep -q .; }

# Host settings
check "KSM off" [ "$(cat /sys/kernel/mm/ksm/run)" = 0 ]
check "token root-only" mode_is /etc/homelab-ci/github-token "root:root 600"
check "repo list root-only" mode_is /etc/homelab-ci/repos "root:root 600"
check "settings dir root-only" mode_is /etc/homelab-ci "root:root 700"
state_dir_ok() { [[ ! -e /run/homelab-ci ]] || mode_is /run/homelab-ci "root:root 700"; }
check "supervisor state dir root-only" state_dir_ok
check "no leftover job registrations" bash -c "! ls /run/homelab-ci/metadata-slot*.json >/dev/null 2>&1"
check "image root-owned, read-only to others" mode_is "$images/rootfs.ext4" "root:root 644"
# The folder too: find walks into it, so a writable folder or file fails.
for file in "$lib" /etc/systemd/system/homelab-ci-net.service \
  /etc/systemd/system/homelab-ci-supervisor.service /usr/local/bin/firecracker /usr/local/bin/jailer; do
  check "only root can change $file" root_only_writes "$file"
done

# What a slot user (Firecracker after a VM escape) can reach
for user in fc-slot0 fc-slot1; do
  if ! id "$user" >/dev/null 2>&1; then
    fail "$user exists"
    continue
  fi
  other=fc-slot$((1 - ${user#fc-slot}))
  for path in /etc/homelab-ci/github-token /etc/homelab-ci/repos /run/homelab-ci /var/run/docker.sock \
    /home/colin /home/colin/actions-runner /etc/shadow /root "/srv/jailer/firecracker/ci-${other#fc-}"; do
    check "$user cannot read $path" bash -c "! sudo -u $user test -r '$path'"
  done
  check "$user cannot write the image" bash -c "! sudo -u $user test -w '$images/rootfs.ext4'"
  check "$user has no login shell" [ "$(getent passwd "$user" | cut -d: -f7)" = /usr/sbin/nologin ]
  check "$user not in docker, sudo or kvm groups" bash -c "! id -nG $user | grep -qwE 'docker|sudo|kvm|adm|lxd'"
done

# Nothing secret in the image
scan=$(mktemp -d)
if mount -o ro,loop "$images/rootfs.ext4" "$scan"; then
  check "no SSH host keys in image" bash -c "! ls '$scan'/etc/ssh/ssh_host_* >/dev/null 2>&1"
  check "no SSH server in image" [ ! -e "$scan/usr/sbin/sshd" ]
  # A real key is a PEM line of its own; npm's docs only quote one inside strings.
  secrets='github_pat_|ghp_[A-Za-z0-9]{30}|^-----BEGIN ([A-Z]+ )?PRIVATE KEY-----[[:space:]]*$'
  no_secrets() { ! grep -rlIE "$secrets" "$scan/etc" "$scan/home" "$scan/root" "$scan/opt" 2>/dev/null | grep -q .; }
  check "no tokens or private keys in image" no_secrets
  umount "$scan"
else
  fail "image could not be mounted for the secrets scan"
fi
rmdir "$scan"

# A live VM: how Firecracker is confined, and what the guest is told
tmp=$(mktemp -d)
# shellcheck disable=SC2016 # expands inside the VM, not here
probe='curl -sf -X PUT -H "X-metadata-token-ttl-seconds: 60" http://169.254.169.254/latest/api/token > /run/t
echo "MMDS_KEYS $(curl -sf -H "X-metadata-token: $(cat /run/t)" -H "Accept: application/json" http://169.254.169.254/ | jq -c "keys")"
sleep 25'
jq -n --arg probe "$probe" '{task: "probe", probe: $probe}' > "$tmp/metadata.json"
"$run_vm" 0 "$tmp/metadata.json" 90 > "$tmp/console.log" 2>&1 &
for _ in $(seq 1 30); do pid=$(pgrep -x firecracker | head -n 1); [[ -n "$pid" ]] && break; sleep 1; done
sleep 5
if [[ -n "${pid:-}" ]]; then
  status=/proc/$pid/status
  check "Firecracker runs as fc-slot0" [ "$(stat -c %U "/proc/$pid")" = fc-slot0 ]
  check "Firecracker has a syscall filter" grep -qP '^Seccomp:\s+2' "$status"
  check "Firecracker cannot gain privileges" grep -qP '^NoNewPrivs:\s+1' "$status"
  check "Firecracker has no capabilities" grep -qP '^CapEff:\s+0+$' "$status"
  # The jailer pivots into its chroot, so the process's / is the chroot mount.
  root_mount=$(awk '$5 == "/" {print $4}' "/proc/$pid/mountinfo")
  check "Firecracker is chrooted" [ "$root_mount" = /srv/jailer/firecracker/ci-slot0/root ]
  seen=$(find "/proc/$pid/root/" -mindepth 1 -maxdepth 1 -printf '%f\n' | sort | tr '\n' ' ')
  check "Firecracker sees only its own files" \
    [ "$seen" = "dev firecracker firecracker.pid metadata.json rootfs.ext4 run vm.json vmlinuz " ]
  cg=/sys/fs/cgroup/firecracker/ci-slot0
  check "memory capped" [ "$(cat $cg/memory.max)" != max ]
  check "CPU weight below services" [ "$(cat $cg/cpu.weight)" -lt 100 ]
else
  fail "no Firecracker process to inspect"
fi
wait
keys=$(grep -oE 'MMDS_KEYS .*' "$tmp/console.log" | head -n 1 | tr -d '\r')
check "guest is told only its task ($keys)" [ "$keys" = 'MMDS_KEYS ["probe","task"]' ]
rm -rf "$tmp"

# The network, from inside a VM
check "network test" bash -c "RUN_VM='$run_vm' '$here/test-network.sh' >/dev/null 2>&1"

if ((failures)); then
  echo "AUDIT FAILED: $failures checks"
  exit 1
fi
echo "audit passed"
