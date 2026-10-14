# Backups

The [backup stack](../../stacks/backup/compose.yaml) runs restic every night at
02:30 UTC and stores encrypted, deduplicated snapshots in the Cloudflare R2
bucket `beelink-backups`. Everything else on Beelink is either in Git or
disposable.

## What is covered

| Source | Why |
|--------|-----|
| `stacks/home-assistant/config` | Automations, integrations, device pairings, users. Not in Git. |
| `stacks/mqtt/data` | Retained messages and persistent sessions. |
| Grafana volume | Users and UI-made changes (`grafana.db`). Dashboards are in Git. |
| Flight-tracker heatmap volume | Accumulated history that cannot be regenerated. |
| CrowdSec config volume | Bouncer registration and local settings. |
| `~/pc-backups` | Each project's data that is not in Git, sent nightly from the Windows PC (below). |

Left out on purpose: the Home Assistant recorder database (sensor history; a
live copy would be inconsistent), logs, Grafana plugins, Prometheus metrics and
Loki logs. Secrets are rendered from GitHub on each deploy.

Retention is 7 daily, 4 weekly and 6 monthly snapshots; each run also prunes
and verifies the repository. On Sundays the verification is a deep check that
downloads and verifies every stored byte (`restic check --read-data`); a
failure fails the run and so raises **Backup Missed**. Force one with
`docker exec -e DEEP_CHECK=1 backup sh -c '/bin/sh /backup.sh once >/proc/1/fd/1 2>&1'`.
A passing deep check also writes
`/var/lib/homelab-metrics/backup-deep-check.prom`, which the weekly server
health report reads; the container log does not survive a redeploy.

The Grafana **Backups** dashboard shows recent successes, failures, stored
size against the cap, deep checks and the run log.

## From the PC

Any project folder on the Windows PC (`D:\Claude Projects\*`) can opt in with a `backup.toml`
listing its data that is not in Git:

```toml
paths = ["~/.servo/servo.db", "~/.servo/bench"]  # files or folders
exclude = ["shots"]                               # names left out below a folder
```

[`scripts/pc_backup.py`](../../scripts/pc_backup.py) runs nightly on the PC (Task Scheduler,
"Homelab PC backup"): one uncompressed tar per project, SQLite databases copied whole with SQLite's
own backup, sent with Taildrop. No SSH key or extra sign-in. On Beelink the
`backup-pc-receive` timer collects them every 15 minutes into `~/pc-backups`, which the nightly
restic run backs up with everything else, and writes the newest file's time to
`homelab_pc_backup_newest_timestamp_seconds`.

One-time setup on Beelink: `sudo tailscale set --operator=colin`, so the user timer may receive
files. On the PC, `python scripts\pc_backup.py --dry-run` packs without sending; the last run is in
`~/.pc-backup.json`.

## Credentials

Four GitHub Actions secrets feed the stack's `.env`: `R2_ACCOUNT_ID`,
`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (an R2 token scoped to the bucket)
and `RESTIC_PASSWORD`. The restic password is also kept in Bitwarden: without
it the snapshots cannot be decrypted, and GitHub will not show it again.

## Alerting

Each successful run logs `backup ok` and writes its time to
`/var/lib/homelab-metrics/backup.prom`, which Alloy exports to Prometheus as
`homelab_backup_last_success_timestamp_seconds`. The Grafana rule **Backup
Missed** fires to Discord when that is over 26 hours old, which covers failed
runs, a stopped container and a server that was down overnight. It used to
count `backup ok` lines in Loki, but on 2026-10-10 Loki missed the line and
the alert fired while backups were succeeding.

## Size cap

R2 is free up to 10 GB and has no server-side quota, so the script enforces
the limit itself. Before uploading it adds the repository's stored size to
the size of the pending changes; if the total would pass 8 GB
(`MAX_REPO_BYTES`) it uploads nothing, logs `backup SKIPPED`, and the run
counts as failed, so **Backup Missed** fires. **Backup Storage High** warns
earlier, at 5 GB. The bucket's token is scoped to this bucket and nothing
else writes to it.

To recover, find what grew (`docker exec backup restic stats`, then
`restic diff` between snapshots), exclude it in `backup.sh`, and forget the
oversized snapshots with `restic forget --prune <id>`.

## Operating

```bash
docker logs --tail 50 backup                 # Recent runs
docker exec backup sh -c '/bin/sh /backup.sh once >/proc/1/fd/1 2>&1'  # Back up now
docker exec backup restic snapshots          # List snapshots
docker exec backup restic ls latest /data/home-assistant
```

The redirect sends a manual run's output to the container log. A manual run
also updates the metric, so it clears **Backup Missed** like a scheduled run.

Without SSH, run the **Server health** workflow with `backup_now` ticked: it
runs the same command, then reports, printing the backup log if it failed.

## Restoring

Restore into a scratch directory first, then copy what is needed into place
with the owning container stopped.

```bash
# One file or directory from the latest snapshot
docker exec backup restic restore latest --target /tmp/restore \
  --include /data/home-assistant/configuration.yaml
docker cp backup:/tmp/restore ./restore

# Whole Home Assistant config after data loss
docker compose -f stacks/home-assistant/compose.yaml down
docker exec backup restic restore latest --target /tmp/restore \
  --include /data/home-assistant
docker cp backup:/tmp/restore/data/home-assistant/. stacks/home-assistant/config/
docker compose -f stacks/home-assistant/compose.yaml up -d
```

The container cannot change file ownership, so `restic restore` ends with
"There were N errors" even though the files are restored intact; set ownership
when copying them into place.

`/tmp` in the container is memory-backed; for a large restore run restic from
any machine instead, using the same repository URL, R2 keys and password.

On a rebuilt host, deploy the other stacks first so their volumes exist, add
the four secrets, deploy `backup`, then restore as above. Named volumes restore
the same way: stop the owning stack and copy into the volume's mount point.
