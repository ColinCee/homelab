# Deploying services

The [deploy workflow](../../.github/workflows/deploy.yaml) runs on Beelink for
stack, script, or workflow changes pushed to protected `main`. Actions resets
the server checkout to the triggering commit, renders `.env` files from an
explicit secret allowlist, and invokes the shared deployment command. Compose
leaves unchanged containers running. CI and deployment are separate workflows;
require CI on pull requests before merging.

## Ownership and deployment contract

This is a Git-defined homelab with self-contained stacks and one shared
deployment command, not a custom deployment platform. Deployment is
push-triggered: there is no continuous drift reconciliation or automatic
removal of deleted stacks. "GitOps-driven" describes the source-of-truth model,
not a continuously running GitOps controller.

| Owner | Responsibility | Boundary |
|-------|----------------|----------|
| GitHub Actions | Select the exact revision, serialize deploys, supply explicitly allowed secrets, invoke the deployment command. | No stack discovery loops or application-specific operations in workflow YAML. |
| Shared deployment command | Discover stacks, validate inputs, build Compose profiles, apply normal services, install convention-based user units, and report readiness or failure. | No branches on stack names, application logic, Git resets, or host provisioning. |
| `stacks/<name>/` | Own Compose, configuration, secret requirements, operational commands, and scheduled units. | A normal stack needs no custom deployment code. |
| Compose | Build/start services, manage stack-local networks and volumes, and enforce declared dependency health conditions. | Do not reproduce its service model in Python. |
| systemd | Schedule stack-owned operations and manage their process lifecycle. | Deployment installs units; it does not implement a scheduler. |
| Host bootstrap | Prepare Docker, Tailscale, runner access, paths, permissions, and user lingering. | An explicit prerequisite, not something every stack repeats. |

### Stack interface

- `stacks/<name>/compose.yaml` is the required entry point. Discover it without
  a stack registry; support deploying all stacks or explicitly selected names.
- Keep optional `.env.example`, configuration, application code, and operational
  commands beside Compose. An external workflow may trigger stack-owned
  commands without owning their logic.
- Reserve `stacks/<name>/systemd/` for optional user `.service`/`.timer` pairs,
  with stack-prefixed unit names. Discover and install them generically; do not
  introduce arbitrary pre/post-deploy hooks.
- Define build and health requirements in Compose. The shared command must
  handle buildable services consistently, including images used by on-demand
  profiles, without starting ingestion or other one-off jobs during deployment.
- Validate selected stacks and required inputs before applying changes. Use
  bounded readiness waits; a successful start command alone is not application
  readiness. Report partial deployment explicitly rather than implying atomic
  rollback across stacks.

The implementation builds with all Compose profiles enabled, then starts only
the normal services with `up`. This leaves images for any on-demand profile
jobs ready without running one-off operations during deployment. Compose
`--wait` supplies bounded readiness: healthchecks are used where declared and
services without one are considered ready when running. Readiness is not a
full application or endpoint health audit.

Adding an ordinary stack should require only its folder. Adding scheduled work
should require only stack-owned commands and units. A new secret still requires
explicit provisioning and an allowlisted workflow binding: plug-and-play does
not mean exposing all repository secrets to every stack. New host privileges or
network access also remain deliberate changes.

Keep Git as the authority for deployed image digests. External application CI
publishes an image; a digest update in this repository goes through CI and merge
before deployment. Do not add an independent image updater that bypasses this
path. Keep destructive removal explicit so deleting a folder cannot silently
delete persistent data.

Use typed Python for substantial validation or operational logic, using the
existing uv, ty, Ruff, and pytest tooling. Small command-only shell wrappers are
acceptable. Choose language by responsibility, not a requirement to rewrite
every script.

## Trust boundary

The runner can execute host-level commands through Docker and workflows.
Repository write access is therefore privileged, even for private repositories.
Keep PR CI on GitHub-hosted runners and Beelink deployment restricted to trusted
`main`. Renovate auto-merges eligible dependency PRs after checks; other changes
are human-directed. Admins can bypass the ruleset, including via agents using
their credentials. Renovate has no such bypass. Workflow conditions alone are
not access controls.

This avoids giving hosted CI tailnet access or maintaining another deployment
platform. Admin ports stay bound to Tailscale. Secrets are explicitly passed
to Compose, never sourced as shell code.

## Manual deployment

On Beelink, select the intended checkout yourself, then:

```bash
cd /home/colin/code/homelab
scripts/deploy.sh                              # Discover and deploy all stacks
scripts/deploy.sh observability                 # Deploy one stack
scripts/deploy.sh observability crowdsec        # Deploy selected stacks
scripts/deploy.sh --readiness-timeout 180 observability
```

The command validates every selected Compose file, `.env`, and systemd pair
before building or starting anything. It uses existing per-stack `.env` files
and never resets Git. A stack failure does not roll back earlier stacks; the
command attempts the remaining selections, prints an explicit partial result,
and exits nonzero. To render new files, export only the values named by the
stack's `.env.example` and run `scripts/generate-env.sh <stack>`. The renderer
writes Docker Compose dotenv data atomically with mode `0600`; never source a
generated `.env` as shell.

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

Deployment discovers user-unit pairs under
`stacks/<name>/systemd/<stack>-*.service` and `.timer`, copies them atomically,
reloads the user manager, and enables each timer. It does not remove unmanaged
units or run arbitrary stack hooks.

For externally built services, CI in the source repo publishes to GHCR.
The flight-tracker image reference includes a digest: pulling it does not
follow changes to the `latest` tag. The old `flight-tracker-poll.timer` was
therefore redundant and is no longer installed. A new image requires updating
the digest in Git; Git and Renovate own that update path.

Do not copy the image-polling pattern for new services. User timers require
`loginctl enable-linger colin` on the host to run without a login session.

### One-time retirement of the old flight-tracker timer

Deployment deliberately does not delete user units it did not discover: that
would make a Git deletion silently destructive. Before or during the first
rollout of this cleanup, inspect and explicitly retire the old timer on
Beelink:

```bash
systemctl --user disable --now flight-tracker-poll.timer
rm -f ~/.config/systemd/user/flight-tracker-poll.service \
      ~/.config/systemd/user/flight-tracker-poll.timer
systemctl --user daemon-reload
systemctl --user list-timers flight-tracker-poll.timer
```

The final command should report no installed timer. If the unit was never
installed, still check the user-unit directory before removing anything. If
`XDG_CONFIG_HOME` is set on the host, use its `systemd/user` directory instead
of `~/.config/systemd/user`.

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
Disable its workflows/timers first, explicitly retire any old user units, then
stop and remove only confirmed containers. Preserve volumes until their data is
deliberately retired or backed up; do not use broad Docker pruning.

Remove unused credentials, network grants, and unmanaged Grafana dashboards
after checking for shared use. Remove the stack and any special deployment
handling from Git last, so the configuration remains available during shutdown.
