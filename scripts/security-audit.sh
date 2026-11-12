#!/usr/bin/env bash
# Prints what to compare with the expected results in the private notes. Read-only: an audit that changes the host
# destroys the evidence it looks for. Run on the Beelink from a checkout of main.
set -uo pipefail

section() { printf '\n== %s\n' "$1"; }

section "Login shells"
getent passwd | awk -F: '$7 !~ /(nologin|false|sync)$/'
section "SSH keys not on GitHub (should be empty)"
comm -23 <(awk '{print $1, $2}' ~/.ssh/authorized_keys | sort) <(curl -s https://github.com/ColinCee.keys | sort)
section "sudoers"
sudo sh -c 'cat /etc/sudoers.d/*'
section "Logins"
last -Fai | head -50
sudo lastb -Fai | head -20
section "Boots (a gap without a clean shutdown is odd)"
journalctl --list-boots | tail

section "Persistence"
sudo ls -la /etc/cron.* /var/spool/cron/crontabs
crontab -l
systemctl list-unit-files --state=enabled --no-pager
systemctl --user list-timers --all --no-pager
cat /etc/ld.so.preload 2>/dev/null
section "setuid programs no package owns (should be empty)"
# Packages may list /usr/bin/x as /bin/x, so try both.
sudo find / -xdev -path /var/lib/docker -prune -o -perm -4000 -print 2>/dev/null |
  while read -r f; do
    dpkg -S "$f" >/dev/null 2>&1 || dpkg -S "/${f#/usr/}" >/dev/null 2>&1 || echo "$f"
  done

section "Kernel"
cat /proc/sys/kernel/tainted
lsmod | sort

section "Network"
sudo ss -tulpn
sudo ss -tnp state established
tailscale status

section "Changed package files (config files are expected)"
sudo dpkg --verify | grep -v ' c /'

section "Containers (each should be a stacks/*/compose.yaml service)"
docker ps -a --format '{{.Names}} {{.Image}} {{.Status}}'

section "Runners on the host (should be none), Firecracker (only while a job runs), KSM (should be 0)"
ls -d ~/actions-runner*
pgrep -a firecracker
cat /sys/kernel/mm/ksm/run
sudo nft list table inet homelab_ci | head -3
sudo nft list table inet homelab_wall | head -3
# ci-runner/tests/audit.sh needs the CI supervisor stopped, so it runs after upgrades, not here.
sudo ci-runner/tests/test-wall.sh

if gh auth status >/dev/null 2>&1; then
  section "GitHub: runners, collaborators, hooks, deploy keys"
  gh api repos/ColinCee/homelab/actions/runners --jq '.runners[].name'
  gh api repos/ColinCee/homelab/collaborators --jq '.[].login'
  gh api repos/ColinCee/homelab/hooks --jq length
  gh api repos/ColinCee/homelab/keys --jq length
  section "Runners on other repos (should be empty): a runner on the Beelink would hold its secrets and home network"
  gh api "users/ColinCee/repos?per_page=100" --jq '.[].name' | grep -vx homelab |
    xargs -I{} gh api repos/ColinCee/{}/actions/runners --jq '.runners[] | "{} \(.name)"'
else
  section "GitHub checks skipped: run this where gh is signed in"
fi
