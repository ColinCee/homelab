#!/usr/bin/env bash
set -euo pipefail

# Report Beelink's health and public site uptime as a Markdown table (to the job summary in Actions).
#   server-health.sh                report only
#   server-health.sh --maintenance  also prune unused Docker images and build
#                                   cache, and test-restore a file from backup
out="${GITHUB_STEP_SUMMARY:-/dev/stdout}"
status=0

row() { printf '| %s | %s |\n' "$1" "$2" >>"$out"; }
bad() {
  row "$1" "FAIL: $2"
  status=1
}

printf '## Beelink health\n\n| Check | Result |\n|---|---|\n' >>"$out"

row "Uptime" "$(uptime -p)"
row "Root disk" "$(df -h / | awk 'NR==2 {print $3 " used of " $2 " (" $5 ")"}')"

backup_log="$(docker logs --since 192h backup 2>&1 || true)"
if docker logs --since 26h backup 2>&1 | grep -qx 'backup ok'; then
  row "Last backup" "ok within 26h"
else
  bad "Last backup" "no 'backup ok' in the last 26h"
fi
size_line="$(grep '^repository size:' <<<"$backup_log" | tail -n 1 || true)"
row "R2 repository" "${size_line:-no size logged in 8 days}"
if grep -qx 'deep check ok' <<<"$backup_log"; then
  row "Weekly deep check" "ok"
else
  bad "Weekly deep check" "no 'deep check ok' in 8 days"
fi

# Filters on the same key are ORed but different keys are ANDed, so query twice.
unhealthy="$({
  docker ps --filter health=unhealthy --format '{{.Names}} ({{.Status}})'
  docker ps -a --filter status=exited --filter status=restarting --format '{{.Names}} ({{.Status}})'
} | paste -sd, -)"
if [[ -n "$unhealthy" ]]; then
  bad "Containers" "$unhealthy"
else
  row "Containers" "$(docker ps -q | wc -l) running, none unhealthy"
fi

for url in https://colincheung.dev https://flight-tracker-at-home.pages.dev \
  https://api.colincheung.dev/health; do
  result="$(curl -4 -s -o /dev/null -m 15 -w '%{http_code} in %{time_total}s' "$url" || true)"
  if [[ "$result" == 200* ]]; then
    row "$url" "$result"
  else
    bad "$url" "${result:-no response}"
  fi
done

upgrades="$(apt-get -s upgrade 2>/dev/null | grep -c '^Inst ' || true)"
row "Pending apt upgrades" "$upgrades"
if [[ -f /var/run/reboot-required ]]; then
  row "Reboot required" "yes (unattended-upgrades reboots at 04:00 UTC)"
else
  row "Reboot required" "no"
fi

if [[ "${1:-}" == "--maintenance" ]]; then
  # Images are re-pulled on deploy; volumes are never touched here.
  images="$(docker image prune -af --filter until=168h | tail -n 1)"
  row "Image prune" "$images"
  cache="$(docker builder prune -af --filter until=168h | tail -n 1)"
  row "Build cache prune" "$cache"

  # restic exits non-zero because the container cannot chown restored files,
  # so success is judged by the restored file, not the exit code.
  file=/data/home-assistant/configuration.yaml
  if docker exec backup sh -c "
    rm -rf /tmp/restore-test
    restic restore latest --target /tmp/restore-test --include $file >/dev/null 2>&1
    test -s /tmp/restore-test$file; ok=\$?
    rm -rf /tmp/restore-test
    exit \$ok"; then
    row "Restore test" "restored $file from latest snapshot"
  else
    bad "Restore test" "could not restore $file"
  fi
fi

exit "$status"
