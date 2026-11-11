#!/bin/sh
# Back up /data to the restic repository, nightly or once.
#   backup.sh loop   wait for BACKUP_TIME (UTC) each day, then back up
#   backup.sh once   back up now
set -eu

# R2 bills past 10 GB and has no server-side quota, so the cap is enforced
# here: a run that would take the repository over this size uploads nothing.
MAX_REPO_BYTES="${MAX_REPO_BYTES:-8000000000}"

# The repository and its S3 login, in the files restic reads (compose.yaml), from the R2 secrets.
(
  umask 077
  account=$(cat /run/secrets/r2_account_id)
  key=$(cat /run/secrets/r2_access_key_id)
  secret=$(cat /run/secrets/r2_secret_access_key)
  echo "s3:https://$account.r2.cloudflarestorage.com/beelink-backups" >"$RESTIC_REPOSITORY_FILE"
  printf '[default]\naws_access_key_id = %s\naws_secret_access_key = %s\n' "$key" "$secret" \
    >"$AWS_SHARED_CREDENTIALS_FILE"
)

backup_data() {
  # The live recorder database, logs, and re-downloadable plugins are left out:
  # a live copy of the database would be inconsistent, so history-snapshot.db
  # (stacks/home-assistant/systemd) is backed up instead.
  restic backup /data \
    --host beelink \
    --exclude "/data/home-assistant/home-assistant_v2.db*" \
    --exclude "/data/home-assistant/*.log*" \
    --exclude "/data/home-assistant/.cache" \
    --exclude "/data/home-assistant/deps" \
    --exclude "/data/home-assistant/tts" \
    "$@"
}

json_number() {
  sed -n "s/.*\"$1\":\([0-9][0-9]*\).*/\1/p" | head -n 1
}

repo_bytes() {
  restic stats --mode raw-data --json | json_number total_size
}

within_cap() {
  stored=$(repo_bytes)
  pending=$(backup_data --dry-run --json 2>/dev/null |
    grep '"message_type":"summary"' | json_number data_added)
  if [ -z "$stored" ] || [ -z "$pending" ]; then
    echo "backup SKIPPED: could not measure repository or upload size" >&2
    return 1
  fi
  if [ $((stored + pending)) -gt "$MAX_REPO_BYTES" ]; then
    echo "backup SKIPPED: $stored bytes stored + $pending new would exceed the $MAX_REPO_BYTES byte cap" >&2
    return 1
  fi
}

verify() {
  # A plain check only reads metadata. Once a week, download and verify every
  # stored byte so silent corruption in R2 is found before a restore needs it.
  if [ "$(date -u +%u)" = "${DEEP_CHECK_DAY:-7}" ] || [ "${DEEP_CHECK:-}" = 1 ]; then
    restic check --read-data && echo "deep check ok" && {
      write_metric "$DEEP_CHECK_FILE" homelab_backup_last_deep_check_timestamp_seconds "$(date -u +%s)" ||
        echo "could not write $DEEP_CHECK_FILE" >&2
    }
  else
    restic check
  fi
}

# step names the stage that is running, so a failed run records where it stopped.
run_backup() {
  step=cap && within_cap &&
    step=backup && backup_data &&
    step=forget && restic forget --host beelink --prune \
      --keep-daily 7 --keep-weekly 4 --keep-monthly 6 &&
    step=check && verify &&
    record_size
}

# The Backup storage nearly full alert reads the metric; server-health.sh the log line.
record_size() {
  bytes=$(repo_bytes) && [ -n "$bytes" ] || return 0
  echo "repository size: $bytes bytes of $MAX_REPO_BYTES allowed"
  write_metric "$SIZE_FILE" homelab_backup_repository_bytes "$bytes" ||
    echo "could not write $SIZE_FILE" >&2
}

report() {
  if run_backup; then
    echo "backup ok"
    write_metric "$METRICS_FILE" homelab_backup_last_success_timestamp_seconds "$(date -u +%s)" ||
      echo "could not write $METRICS_FILE" >&2
    rm -f "$FAILURE_FILE"
  else
    echo "backup FAILED at $step" >&2
    write_metric "$FAILURE_FILE" "homelab_backup_last_failure_timestamp_seconds{step=\"$step\"}" "$(date -u +%s)" ||
      echo "could not write $FAILURE_FILE" >&2
    return 1
  fi
}

# Alloy reads this file into Prometheus for the Nightly backup didn't run alert. A metric,
# not the "backup ok" log line: a log store missed that line once and the alert fired with
# backups succeeding.
METRICS_FILE=/metrics/backup.prom
# The last run's failing step, for the alert to name; removed by the next good run.
FAILURE_FILE=/metrics/backup-failure.prom
# The weekly report reads this, not the log, for the same reason.
DEEP_CHECK_FILE=/metrics/backup-deep-check.prom
SIZE_FILE=/metrics/backup-size.prom

# write_metric FILE NAME VALUE
write_metric() {
  printf '%s %s\n' "$2" "$3" >"$1.tmp" && mv "$1.tmp" "$1"
}

# On a fresh host, start from the newest snapshot so the alert has a value
# before the first nightly run.
seed_metric() {
  [ -f "$METRICS_FILE" ] && return
  latest=$(restic snapshots --host beelink --latest 1 --json |
    sed -n 's/.*"time":"\([0-9-]*\)T\([0-9:]*\).*/\1 \2/p' | head -n 1)
  [ -n "$latest" ] && write_metric "$METRICS_FILE" homelab_backup_last_success_timestamp_seconds \
    "$(date -u -d "$latest" +%s)"
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
  seed_metric || echo "could not seed $METRICS_FILE" >&2
  touch /tmp/ready
  [ -f "$SIZE_FILE" ] || record_size
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
