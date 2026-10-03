#!/bin/sh
# Back up /data to the restic repository, nightly or once.
#   backup.sh loop   wait for BACKUP_TIME (UTC) each day, then back up
#   backup.sh once   back up now
set -eu

run_backup() {
  # The recorder database, logs, and re-downloadable plugins are left out: a
  # live copy of the database would be inconsistent and it dominates churn.
  restic backup /data \
    --host beelink \
    --exclude "/data/home-assistant/home-assistant_v2.db*" \
    --exclude "/data/home-assistant/*.log*" \
    --exclude "/data/home-assistant/.cache" \
    --exclude "/data/home-assistant/deps" \
    --exclude "/data/home-assistant/tts" \
    --exclude "/data/grafana/plugins" \
    --exclude "/data/grafana/png" \
    --exclude "/data/grafana/csv" \
    --exclude "/data/grafana/pdf" &&
    restic forget --host beelink --prune \
      --keep-daily 7 --keep-weekly 4 --keep-monthly 6 &&
    restic check
}

report() {
  if run_backup; then
    # The Grafana missed-backup alert counts this line; keep the text stable.
    echo "backup ok"
  else
    echo "backup FAILED" >&2
    return 1
  fi
}

seconds_until() {
  now=$(date -u +%s)
  target=$(date -u -d "$(date -u +%Y-%m-%d) $1:00" +%s)
  if [ "$target" -le "$now" ]; then
    target=$((target + 86400))
  fi
  echo $((target - now))
}

# Create the repository on first use; fail fast on bad credentials.
restic cat config >/dev/null 2>&1 || restic init

case "${1:-loop}" in
once)
  report
  ;;
loop)
  touch /tmp/ready
  while true; do
    sleep "$(seconds_until "${BACKUP_TIME:-02:30}")"
    report || true
  done
  ;;
*)
  echo "usage: backup.sh [loop|once]" >&2
  exit 2
  ;;
esac
