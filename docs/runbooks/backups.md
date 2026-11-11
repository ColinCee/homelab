# Backups

The [backup stack](../../stacks/backup/compose.yaml) runs restic every night at
02:30 UTC and stores encrypted, deduplicated snapshots in the Cloudflare R2
bucket `beelink-backups`. Everything else on Beelink is either in Git or
disposable.

## What is covered

| Source | Why |
|--------|-----|
| `stacks/home-assistant/config` | Automations, integrations, device pairings, users. Not in Git. Sensor history as `history-snapshot.db`, a consistent copy `home-assistant-history.timer` makes at 02:10 UTC. |
| Flight-tracker heatmap volume | Accumulated history that cannot be regenerated. |
| `~/pc-backups` | Each project's data that is not in Git, sent nightly from the Windows PC (below). |

Left out on purpose: the live Home Assistant recorder database (a live copy would be
inconsistent; the snapshot above stands in for it), logs, and stored metrics and logs. Secrets come
from GitHub.

Retention is 7 daily, 4 weekly and 6 monthly snapshots; each run also prunes
and verifies the repository. On Sundays the verification is a deep check that
downloads and verifies every stored byte (`restic check --read-data`); a
failure fails the run and so raises **Nightly backup didn't run**. Force one with
`docker exec -e DEEP_CHECK=1 backup sh -c '/bin/sh /backup.sh once >/proc/1/fd/1 2>&1'`.
A passing deep check also writes
`/var/lib/homelab-metrics/backup-deep-check.prom`, which the weekly server
health report reads; the container log does not survive a redeploy.

The **Backups** row of Grafana's Home shows recent successes, failures, stored
size against the cap, deep checks and the run log.

## From the PC

Any project folder on the Windows PC can opt in with a `backup.toml` listing its data that is not in
Git; the format and what is sent are in [`scripts/pc_backup.py`](../../scripts/pc_backup.py), which runs
nightly on the PC (Task Scheduler, "Homelab PC backup"). On Beelink the
`backup-pc-receive` timer collects them every 15 minutes into `~/pc-backups`, which the nightly
restic run backs up with everything else, and writes the newest file's time to
`homelab_pc_backup_newest_timestamp_seconds`; **PC backup stopped arriving** fires
when that is over 50 hours old.

One-time setup on Beelink: `sudo tailscale set --operator=colin`, so the user timer may receive
files. On the PC, `python scripts\pc_backup.py --dry-run` packs without sending; the last run is in
`~/.pc-backup.json`.

## Credentials

Four GitHub Actions secrets feed the stack's secret files: `R2_ACCOUNT_ID`,
`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (an R2 token scoped to the bucket)
and `RESTIC_PASSWORD`. The restic password is also kept in Bitwarden: without
it the snapshots cannot be decrypted, and GitHub will not show it again.

## Alerting and the size cap

**Nightly backup didn't run** fires when no run has succeeded for 26 hours, which covers failed runs, a stopped
container and a server that was down overnight; a failed run names its step. It reads the time `backup.sh` writes
to `/var/lib/homelab-metrics/`: do not alert on `backup ok` log lines instead, since a log store can miss a line
and fire while backups succeed.

R2 is free up to 10 GB and has no server-side quota, so `backup.sh` uploads nothing past its `MAX_REPO_BYTES` (8 GB)
and the run counts as failed; **Backup storage nearly full** warns earlier. To recover, find what grew
(`docker exec backup restic stats`, then `restic diff` between snapshots), exclude it in `backup.sh`, and forget the
oversized snapshots with `restic forget --prune <id>`.

## Operating

```bash
docker logs --tail 50 backup                 # Recent runs
docker exec backup sh -c '/bin/sh /backup.sh once >/proc/1/fd/1 2>&1'  # Back up now
docker exec backup restic snapshots          # List snapshots
docker exec backup restic ls latest /data/home-assistant
```

The redirect sends a manual run's output to the container log. A manual run
also updates the metric, so it clears **Nightly backup didn't run** like a scheduled run.

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
