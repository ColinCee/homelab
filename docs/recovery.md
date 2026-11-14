# Recovery

Hand steps for when the automation cannot fix it. How the parts fit: [architecture](architecture.md).

## Restoring from backup

Restore into a scratch folder, then copy into place with the owning stack stopped:

```bash
docker exec backup restic restore latest --target /tmp/restore --include /data/home-assistant
docker compose -f stacks/home-assistant/compose.yaml down
docker cp backup:/tmp/restore/data/home-assistant/. stacks/home-assistant/config/
docker compose -f stacks/home-assistant/compose.yaml up -d
```

restic ends with "There were N errors" because the container cannot set file owners; the files are intact, so set
ownership when copying. `/tmp` in the container is memory: for a large restore, run restic from another machine with
the same repository URL, R2 keys and password. Named volumes restore the same way, into the volume's mount point.

## Backup store full

Uploads stop at 8 GB, so every night fails until it shrinks: find what grew (`docker exec backup restic stats`, then
`restic diff` between snapshots), exclude it in [`backup.py`](../stacks/backup/backup.py), and drop the oversized
snapshots with `restic forget --prune <id>`.

## Rebuilding the Beelink

1. Ubuntu, Tailscale (`sudo tailscale set --operator=colin`, so Taildrop backups arrive), Docker, and
   `loginctl enable-linger colin`.
2. From a clone of `main` at `/home/colin/code/homelab`, once:

   ```bash
   sudo install -D -m 644 beelink/docker/tailscale.conf /etc/systemd/system/docker.service.d/tailscale.conf
   sudo install -m 644 beelink/auto-updates/52unattended-upgrades-local /etc/apt/apt.conf.d/
   sudo install -m 644 beelink/auto-updates/docker-major.pref /etc/apt/preferences.d/
   sudo systemctl daemon-reload
   ```

3. Copy each line of the Bitwarden note "Homelab secrets" into `secrets/<name>` ([list](#secrets); folder 700, files 444), then
   `stacks/deploy/deploy.sh --last-deployed ~/.local/state/homelab/last-deployed`: every stack comes up empty; `deploy-main`
   follows. Deploy `backup` last and restore as above.
4. CI runner: `sudo ci-runner/install_host.py`, `sudo ci-runner/build-rootfs.sh`, `sudo ci-runner/host-tests/test_tools.py`, `sudo ci-runner/host-tests/audit.py`
   (must pass), store the GitHub token as the supervisor's `TOKEN_FILE` says, then
   `sudo systemctl enable --now homelab-ci-supervisor.service`.

If the Tailscale address changes, update the Docker drop-in, the Compose port bindings, `stacks/internal-names` and
the `.internal` nameserver in Tailscale's DNS settings.

## Secrets

Names are public; values live in Bitwarden's "Homelab secrets". `checks/secrets_listed_test.py` keeps this in step.

| `secrets/` file | Used by | Gives |
| --- | --- | --- |
| `agents_password` | observability | Agents' read-only gateway login |
| `cf_tunnel_token` | tunnel | Runs the Cloudflare Tunnel |
| `grafana_admin_password` | observability | Grafana's admin |
| `grafana_secret_key` | observability | Grafana's signing key |
| `healthchecks_ping_url` | observability | Incidents relay's dead-man ping |
| `homeassistant_metrics_token` | observability | Reads Home Assistant's metrics |
| `incidents_discord_bot_token` | observability | The Homelab Discord bot |
| `oncall_fire_token` | observability | Starts the on-call routine |
| `public_stats_key` | observability | Signs the public stats page |
| `r2_access_key_id` | backup | R2 login, with the next two |
| `r2_account_id` | backup | |
| `r2_secret_access_key` | backup | |
| `restic_password` | backup | Decrypts every backup |
| `stats_password` | observability | Public stats' gateway login |

Elsewhere: the CI runner's `/etc/homelab-ci/github-token`, and the claude.ai Homelab environment's network secrets
for `observe.colincheung.dev` (the gateway login and a Cloudflare Access service token) and `discord.com` (the bot token).

## CI runner rollback

`sudo ROLLBACK=1 /usr/local/lib/homelab-ci/update.py <commit>` pins the host to that commit until
`sudo rm /etc/homelab-ci/pinned`. After a kernel upgrade, rebuild the image: `sudo ci-runner/build-rootfs.sh`.

## Containers without networking

When Docker started before the Tailscale address, recreate only the stranded services:

```bash
docker compose --env-file stacks/observability/settings.env -f stacks/observability/compose.yaml \
  up -d --force-recreate --no-deps grafana gateway
```

## Suspected compromise

Run `beelink/security/security-audit.sh` on the host, read-only so it keeps the evidence, and compare with the expected results
in the private notes.
