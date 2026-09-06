# Deploying services

The [deploy workflow](../../.github/workflows/deploy.yaml) runs on Beelink for
stack, script, or workflow changes pushed to protected `main`. It resets the
server checkout to the triggering commit, renders `.env` files from explicitly
named GitHub secrets, and reconciles all stacks. Compose leaves unchanged
containers running. CI and deployment are separate workflows; require CI on
pull requests before merging.

## Trust boundary

The runner can execute host-level commands through Docker and workflows.
Repository write access is therefore privileged, even for private repositories.
Keep PR CI on GitHub-hosted runners and Beelink deployment restricted to trusted
`main`. Protect that branch and require human approval before merge; workflow
conditions alone are not access controls.

This avoids giving hosted CI tailnet access or maintaining another deployment
platform. Admin ports stay bound to Tailscale. Secrets are explicitly passed
to Compose, never sourced as shell code.

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

For externally built services, CI in the source repo publishes to GHCR.
The flight-tracker timer pulls the backend image every 30 seconds and reconciles
it with Compose. Polling avoids cross-repo dispatch credentials and an update
service with Docker socket access; it does not make the image untrusted-code
safe. Whoever can publish the selected image can change what runs on Beelink.

To add another polled service, follow the flight-tracker `.service`/`.timer`
units and add its timer installation to `deploy.sh`. User timers require
`loginctl enable-linger colin` on the host to run without a login session.

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

## Remove a service

Deleting a stack directory does not stop its containers or installed timers.
Disable its workflows/timers first, then stop and remove only confirmed
containers. Preserve volumes until their data is deliberately retired or backed
up; do not use broad Docker pruning.

Remove unused credentials, network grants, and unmanaged Grafana dashboards
after checking for shared use. Remove the stack and any special deployment
handling from Git last, so the configuration remains available during shutdown.
