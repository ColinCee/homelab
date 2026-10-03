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

Left out on purpose: the Home Assistant recorder database (sensor history; a
live copy would be inconsistent), logs, Grafana plugins, Prometheus metrics and
Loki logs. Secrets are rendered from GitHub on each deploy.

Retention is 7 daily, 4 weekly and 6 monthly snapshots; each run also prunes
and verifies the repository.

## Credentials

Four GitHub Actions secrets feed the stack's `.env`: `R2_ACCOUNT_ID`,
`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (an R2 token scoped to the bucket)
and `RESTIC_PASSWORD`. The restic password is also kept in Bitwarden: without
it the snapshots cannot be decrypted, and GitHub will not show it again.

## Alerting

Each successful run logs `backup ok`. The Grafana rule **Backup Missed** fires
to Discord when Loki has seen no such line for 26 hours, which covers failed
runs, a stopped container and a server that was down overnight.

## Operating

```bash
docker logs --tail 50 backup                 # Recent runs
docker exec backup /bin/sh /backup.sh once   # Back up now
docker exec backup restic snapshots          # List snapshots
docker exec backup restic ls latest /data/home-assistant
```

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
