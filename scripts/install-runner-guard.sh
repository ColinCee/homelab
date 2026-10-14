#!/usr/bin/env bash
# Install the runner job guard on Beelink as root-owned files, then point the deploy
# runner's job-started hook at it. Run from a checkout of main:
#   sudo scripts/install-runner-guard.sh observe   # log only, for the first rollout
#   sudo scripts/install-runner-guard.sh enforce
# Restart the runner afterwards; it reads .env only at start.
set -euo pipefail

mode=${1:?usage: install-runner-guard.sh observe|enforce}
case "$mode" in observe | enforce) ;; *) echo "mode must be observe or enforce" >&2; exit 2 ;; esac
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

here=$(cd "$(dirname "$0")" && pwd)
dest=/usr/local/lib/homelab
runner_env=${RUNNER_ENV:-/home/colin/actions-runner/.env}
hook_line="ACTIONS_RUNNER_HOOK_JOB_STARTED=$dest/runner-job-guard.sh"

install -d -o root -g root -m 0755 "$dest"
install -o root -g root -m 0755 "$here/runner_job_guard.py" "$dest/runner_job_guard.py"
sed "s/^MODE=.*/MODE=$mode/" "$here/runner-job-guard.sh" > "$dest/runner-job-guard.sh.new"
chown root:root "$dest/runner-job-guard.sh.new"
chmod 0755 "$dest/runner-job-guard.sh.new"
mv "$dest/runner-job-guard.sh.new" "$dest/runner-job-guard.sh"

if ! grep -qxF "$hook_line" "$runner_env"; then
  sed -i '/^ACTIONS_RUNNER_HOOK_JOB_STARTED=/d' "$runner_env"
  echo "$hook_line" >> "$runner_env"
fi

echo "Installed guard in $mode mode. Restart the runner when no deploy is running:"
echo "  sudo systemctl restart actions.runner.ColinCee-homelab.beelink.service"
