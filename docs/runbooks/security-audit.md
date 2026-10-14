# Security audit

How to check Beelink for compromise. Run it read-only: an audit that changes
the host destroys the evidence it is looking for. The expected results, past
findings and accepted risks are kept privately; compare against those.

## Checklist

On Beelink, with `sudo` where needed.

```bash
# Accounts, keys, logins
getent passwd | awk -F: '$7 !~ /(nologin|false|sync)$/'
# Keys here that are not on GitHub; should print nothing
comm -23 <(awk '{print $1, $2}' ~/.ssh/authorized_keys | sort) \
  <(curl -s https://github.com/ColinCee.keys | sort)
sudo cat /etc/sudoers.d/*
last -Fai | head -50; sudo lastb -Fai | head -20
journalctl --list-boots | tail     # gaps without a clean shutdown

# Persistence
sudo ls -la /etc/cron.* /var/spool/cron/crontabs
systemctl list-unit-files --state=enabled --no-pager
systemctl --user list-timers --all --no-pager
cat /etc/ld.so.preload 2>/dev/null
# setuid programs no package owns; should print nothing. Packages may list
# /usr/bin/x as /bin/x, so try both.
sudo find / -xdev -path /var/lib/docker -prune -o -perm -4000 -print 2>/dev/null |
  while read -r f; do
    dpkg -S "$f" >/dev/null 2>&1 || dpkg -S "/${f#/usr/}" >/dev/null 2>&1 || echo "$f"
  done

# Kernel
cat /proc/sys/kernel/tainted
lsmod | sort

# Network
sudo ss -tulpn
sudo ss -tnp state established
tailscale status

# Packages: changed files from installed packages (config files are expected)
sudo dpkg --verify | grep -v ' c /'

# Containers: each should match a stacks/*/compose.yaml service and image
docker ps -a --format '{{.Names}} {{.Image}} {{.Status}}'

# Runner
journalctl -t homelab-runner-guard --since -30d | grep verdict=refuse   # each one explained?
ls -d ~/actions-runner*

# Firecracker CI runner (see ci-runner.md)
cat /sys/kernel/mm/ksm/run                      # 0
pgrep -a firecracker                            # only while a job runs
sudo nft list table inet homelab_ci | head -3   # present
sudo ci-runner/tests/audit.sh                   # passes
```

From any machine with `gh`:

```bash
gh api repos/ColinCee/homelab/actions/runners --jq '.runners[].name'
gh api repos/ColinCee/homelab/collaborators --jq '.[].login'
gh api repos/ColinCee/homelab/hooks --jq length
gh api repos/ColinCee/homelab/keys --jq length
gh api "users/ColinCee/repos?per_page=100" --jq '.[].name' |
  xargs -I{} gh api repos/ColinCee/{}/actions/runners --jq '.runners[] | "{} \(.name)"'
```

The last command must print only `homelab beelink`. A runner on any other
repo has the same power over the host without the
[runner job guard](deploying-services.md#runner-job-guard).

## Lessons

- **Experiments leave residue.** After installing anything to test, check
  processes, `lsmod`, `/proc/sys/kernel/tainted`, `dpkg --verify` and the
  filesystem, then reboot. A cleanup reported as done in October 2026 was not.
- **Unused services are attack surface.** When a stack is retired, also remove
  its runner, credentials and network grants; see
  [Remove a service](deploying-services.md#remove-a-service).
- **Verify a control with a real attempt.** A failing runner hook did not stop
  later `if: always()` steps, which only a real refused run showed.
- **Record answers.** Write down what each odd finding turned out to be, so
  the next audit starts from it.
