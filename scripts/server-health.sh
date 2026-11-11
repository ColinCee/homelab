#!/usr/bin/env bash
set -euo pipefail

# Report Beelink's health and public site uptime as a Markdown table (to the job summary in Actions).
#   server-health.sh                report only
#   server-health.sh --maintenance  also prune unused Docker images and build
#                                   cache, and test-restore a file from backup
# Also printed to the log, which agents can read through the API.
out="${GITHUB_STEP_SUMMARY:-/dev/null}"
status=0

row() { printf '| %s | %s |\n' "$1" "$2" | tee -a "$out"; }
bad() {
  row "$1" "FAIL: $2"
  status=1
}

printf '## Beelink health\n\n| Check | Result |\n|---|---|\n' | tee -a "$out"

row "Uptime" "$(uptime -p)"
row "Root disk" "$(df -h / | awk 'NR==2 {print $3 " used of " $2 " (" $5 ")"}')"

backup_log="$(docker logs --since 192h backup 2>&1 || true)"
# The metric Nightly backup didn't run alerts on, not the container log: recreating the
# container on deploy wipes its log. See docs/runbooks/backups.md.
metric_ts="$(awk '{print $2}' /var/lib/homelab-metrics/backup.prom 2>/dev/null || true)"
if [[ -n "$metric_ts" ]] && (( $(date +%s) - ${metric_ts%.*} < 26 * 3600 )); then
  row "Last backup" "ok at $(date -u -d "@$metric_ts" '+%Y-%m-%d %H:%M UTC')"
else
  last="no metric file"
  [[ -n "$metric_ts" ]] && last="last $(date -u -d "@$metric_ts" '+%Y-%m-%d %H:%M UTC')"
  bad "Last backup" "none in the last 26h ($last)"
  # Log only, not the summary: the failed run's output says why.
  echo "Backup log, last 26h:"
  docker logs --since 26h backup 2>&1 | tail -n 40 || true
fi
size_line="$(grep '^repository size:' <<<"$backup_log" | tail -n 1 || true)"
row "R2 repository" "${size_line:-no size logged in 8 days}"
# The deep check's own metric for the same reason; the log is a fallback until
# the first deep check after this metric was added has run.
deep_ts="$(awk '{print $2}' /var/lib/homelab-metrics/backup-deep-check.prom 2>/dev/null || true)"
if [[ -n "$deep_ts" ]] && (( $(date +%s) - ${deep_ts%.*} < 8 * 86400 )); then
  row "Weekly deep check" "ok at $(date -u -d "@$deep_ts" '+%Y-%m-%d %H:%M UTC')"
elif grep -qx 'deep check ok' <<<"$backup_log"; then
  row "Weekly deep check" "ok"
else
  last="no metric file"
  [[ -n "$deep_ts" ]] && last="last $(date -u -d "@$deep_ts" '+%Y-%m-%d %H:%M UTC')"
  bad "Weekly deep check" "none in 8 days ($last); the log does not survive a redeploy"
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

# Alerts and metrics: Prometheus through the read-only gateway at GATEWAY_URL, with GATEWAY_LOGIN (user:password) as a
# curl config on stdin.
gateway="${GATEWAY_URL:?}"
login() {
  # Quoted patterns: bash 5.2 reads an unquoted `\\\\` replacement as one backslash.
  local pw=${GATEWAY_LOGIN//"\\"/"\\\\"}
  pw=${pw//"\""/"\\\""}
  printf 'user = "%s"\n' "$pw"
}
promql() { # QUERY: the Prometheus API's JSON answer
  login | curl -sf -m 10 -K - -G "$gateway/prometheus/api/v1/query" --data-urlencode "query=$1"
}
# Firing rules by title; one whose query failed, or not run for 5 minutes, is named too.
if ! rules="$(login | curl -sf -m 10 -K - "$gateway/prometheus/api/v1/rules")"; then
  firing="unknown (gateway unreachable)"
elif ! firing="$(jq -r --argjson stale "$(( $(date +%s) - 300 ))" '[.data.groups[].rules[] |
    (.annotations.title // .name) as $rule |
    if .health != "ok" then "\($rule) (query fails)"
    elif (.lastEvaluation | sub("\\.[0-9]+"; "") | fromdateiso8601) < $stale then "\($rule) (not run for 5 min)"
    elif .state == "firing" then $rule
    else empty end] | join(", ")' <<<"$rules" 2>/dev/null)"; then
  firing="unknown (Prometheus's rule list did not parse)"
fi
if [[ -n "$firing" ]]; then
  bad "Firing alerts" "$firing"
else
  row "Firing alerts" "none"
fi

# Log only: why the flight tracker served stale data (upstream fetch failures).
echo "Flight tracker upstream failures, last 24h (count, reason):"
docker logs --since 24h flight-tracker-backend-1 2>&1 | grep 'fetch failed' |
  sed -E 's/^.*fetch failed: //; s/[0-9]+(\.[0-9]+)?s/Ns/g' | sort | uniq -c | sort -rn | head -n 10 || true
echo "Most recent:"
docker logs --since 24h -t flight-tracker-backend-1 2>&1 | grep 'fetch failed' | tail -n 5 || true

# What Flight Tracker site down alerts on: the /aircraft probe must report live data.
aircraft="$(curl -4 -sf -m 15 -w '\n%{http_code} in %{time_total}s' https://api.colincheung.dev/aircraft)" ||
  aircraft=$'\nrequest failed'
health="$(head -n -1 <<<"$aircraft" | grep -o '"apiHealth": *"[a-z]*"' | grep -o '[a-z]*"$' | tr -d '"' || true)"
if [[ "$health" == live && "$(tail -n 1 <<<"$aircraft")" == 200* ]]; then
  row "Flight tracker data" "live, $(tail -n 1 <<<"$aircraft")"
else
  bad "Flight tracker data" "${health:-no apiHealth}, $(tail -n 1 <<<"$aircraft")"
fi
# Log only: how the alert's probe has behaved. Gaps in samples mean failed scrapes,
# which the alert counts as down (no data).
echo "Flight tracker probe, last 24h (expect 1440 samples at one a minute):"
for expr in 'avg_over_time(probe_success{job="integrations/blackbox/flight-tracker"}[24h])' \
  'count_over_time(probe_success{job="integrations/blackbox/flight-tracker"}[24h])' \
  'max_over_time(probe_duration_seconds{job="integrations/blackbox/flight-tracker"}[24h])' \
  'avg_over_time(up{job="integrations/blackbox/flight-tracker"}[24h])'; do
  value="$(promql "$expr" | jq -r '.data.result[0].value[1] // "none"' 2>/dev/null || echo unavailable)"
  echo "  ${expr%%(*}: $value"
done

# Log only: CPU over the last 5 minutes, in cores. Beelink CPU maxed out alerts on the
# whole machine minus iowait and the CI VMs, so busy CI alone does not page.
echo "CPU, last 5m (cores):"
for expr in 'count(node_cpu_seconds_total{mode="idle",job="integrations/unix"})' \
  'sum(rate(node_cpu_seconds_total{mode!="idle",job="integrations/unix"}[5m]))' \
  'sum(rate(container_cpu_usage_seconds_total{job="ci-vms",id=~"/firecracker/ci-slot[0-9]+"}[5m]))'; do
  value="$(promql "$expr" | jq -r '.data.result[0].value[1] // "none"' 2>/dev/null || echo unavailable)"
  echo "  $expr: $value"
done
# Log only: where the rest goes, by CPU mode and by container.
for expr in 'sum by (mode) (rate(node_cpu_seconds_total{mode!="idle",job="integrations/unix"}[5m]))' \
  'topk(6, sum by (name) (rate(container_cpu_usage_seconds_total{job="docker",name!=""}[5m])))'; do
  echo "  $expr:"
  promql "$expr" |
    jq -r '.data.result | sort_by(-(.value[1] | tonumber))[] | "    \(.metric.mode // .metric.name): \(.value[1])"' 2>/dev/null ||
    echo "    unavailable"
done

# CI machines. This log is public, so repository
# names are masked and only counts from the status file are printed.
supervisor="$(systemctl is-active homelab-ci-supervisor 2>/dev/null || true)"
if [[ "$supervisor" == active ]]; then
  row "CI supervisor" "active"
else
  bad "CI supervisor" "${supervisor:-unknown}"
fi
echo "CI machines status:"
jq -r '"  updated \((now - .updated) | floor)s ago, slots \(.slots), in_use \(.in_use), standby \(.standby), github_ok \(.github_ok), mem_available_mib \(.mem_available_mib), held_for_memory \(.held_for_memory)"' \
  /var/lib/homelab-ci/status/machines.json 2>/dev/null || echo "  unavailable"
echo "CI supervisor log, last 2 hours (slow polls left out, last 60 lines, repos masked):"
sudo -n journalctl -u homelab-ci-supervisor --since -2h --no-pager -o short-iso 2>/dev/null |
  { grep -v ': slow poll: ' || true; } | tail -n 60 |
  sed -E 's#[A-Za-z0-9_.-]+(/|__)[A-Za-z0-9_.-]+#<repo>#g; s/^/  /' || echo "  unavailable"

upgrades="$(apt-get -s upgrade 2>/dev/null | grep -c '^Inst ' || true)"
row "Pending apt upgrades" "$upgrades"
# Log only: which packages are waiting, and whether Ubuntu is still phasing them in.
echo "Pending apt upgrades:"
apt-get -s upgrade 2>/dev/null | sed -n '/^The following/,/^[0-9]* upgraded/p' || true
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
