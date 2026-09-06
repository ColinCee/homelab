# Deploying services

The [deploy workflow](../../.github/workflows/deploy.yaml) runs on Beelink for
stack, script, or workflow changes pushed to protected `main`. It resets the
server checkout to the triggering commit, renders `.env` files from explicitly
named GitHub secrets, and reconciles all stacks. Compose leaves unchanged
containers running. CI and deployment are separate workflows; require CI on
pull requests before merging.

## Manual deployment

On Beelink, select the intended checkout yourself, then:

```bash
cd /home/colin/code/homelab
scripts/deploy.sh             # All stacks
scripts/deploy.sh knowledge   # Selected stack
```

The script uses existing per-stack `.env` files and never resets Git. To render
new files, export the secrets named by that stack's `.env.example` and run
`scripts/generate-env.sh <stack>`. Never source a generated `.env` as shell.

## Adding a stack

1. Add `stacks/<name>/compose.yaml` with pinned images, persistent volumes, and
   `restart: unless-stopped`. Bind admin ports to Tailscale; reserve host
   networking for services that need it.
2. If needed, add `.env.example` and explicitly name its secrets in the deploy
   workflow. Do not pass the entire GitHub secrets collection.
3. Run `mise run validate:compose`. Stack discovery is automatic; no task or
   deployment registry needs updating.

Within a stack, use Docker service names. Across stacks, use a host-mapped
Tailscale port rather than assuming shared Docker DNS.

## Timers and dashboards

`scripts/deploy.sh` installs the knowledge backup and flight-tracker image-poll
user timers. Keep those stack-specific actions in the deploy script.
External image hosting is described in [ADR-013](../decisions/013-external-service-hosting.md).

Grafana loads dashboard JSON from the read-only mounted directory. It polls for
updates; no API uploader or separate dashboard-sync command is needed.

## Docker startup ordering

Published admin ports bind to the Tailscale address. Docker must wait for that
address, not merely for `tailscaled.service` to start. Otherwise containers can
fail to start or remain running without their network attachments.

Install the drop-in once on Beelink (requires sudo):

```bash
sudo install -D -m 644 systemd/docker.service.d/tailscale.conf \
  /etc/systemd/system/docker.service.d/tailscale.conf
sudo systemctl daemon-reload
```

This affects the next Docker start; do not restart Docker just to install it.
The bounded wait fails the start if the expected address is missing rather than
publishing services on a public interface. If the server's Tailscale IP changes,
update both this drop-in and the Compose bindings.

For containers already stranded without networking, recreate only affected
services with their existing `.env` and named volumes:

```bash
docker compose --env-file stacks/observability/.env \
  -f stacks/observability/compose.yaml up -d --force-recreate --no-deps grafana
docker compose -f stacks/crowdsec/compose.yaml up -d --force-recreate
```

## One-time retirement of the old agents

Deleting repository files does not stop existing containers. Before considering
the retirement complete:

1. Disable the old implementation/review workflows in GitHub if they are still
   enabled, and stop the API before its workers so it cannot create more.
2. Inspect `docker ps -a --format '{{.ID}} {{.Names}}'`. Remove only the confirmed
   `agents-agent-*` and `worker-implement-*` / `worker-review-*` containers with
   `docker rm -f <confirmed-container-ids>`. Do not prune Docker or delete volumes.
3. Remove the retired agent dashboard in Grafana if the former API-uploaded copy
   remains. File provisioning does not automatically delete old unmanaged dashboards.
4. Revoke agent-only API/App credentials and remove the obsolete port 8585 ACL.
   Check for shared use first; knowledge still uses `COPILOT_GITHUB_TOKEN`.

Keep any transcripts or volumes until deliberately choosing to delete them.
