# Deploying services

A merge to `main` deploys: the [deploy workflow](../../.github/workflows/deploy.yaml) runs on the Beelink, writes
the secrets its render step names to `secrets/`, and runs [`scripts/deploy.sh`](../../scripts/deploy.sh) for the
stacks changed since the last full deploy (the order and the marker are in [`deploy.py`](../../scripts/deploy.py)).
A changed GitHub secret reaches a stack only when that stack next deploys: run `scripts/deploy.sh <stack>` by hand,
or delete `~/.local/state/homelab/last-deployed` so the next push deploys everything. Deleting a stack's folder
removes nothing from the host ([Remove a service](#remove-a-service)).

## Trust boundary

The deploy runner can run anything on the host through Docker, so write access to this repository is privileged.
Only trusted `main` deploys run there (CI's split is in `AGENTS.md`). Admins can bypass the ruleset, agents using
their credentials included; Renovate cannot. Workflow conditions alone are not access controls.

### Runner job guard

The repository is public, so a fork pull request can bring its own workflow
targeting `beelink`; fork runs need approval, and one wrong approval would run
it as `colin`. The runner's job-started hook,
[`runner_job_guard.py`](../../scripts/runner_job_guard.py), refuses every job
except its `WORKFLOWS` from `main` on push, schedule, or
manual dispatch, started by `ColinCee` or `renovate[bot]` (checked by user ID, and the re-run's
triggering actor by name). It runs before the job's first step
and fails closed when a variable is missing.

A failing hook alone only marks the job failed: the runner still runs later
steps whose condition allows it, such as `if: always()`, which was confirmed on
this runner. On refusal the guard therefore also kills the job's
`Runner.Worker`, so nothing after the hook runs; GitHub marks the job failed
within seconds and the runner stays online.

The installed copy is root-owned in `/usr/local/lib/homelab/` and deploys never
update it, so a push to `main` cannot change the policy. Adding a workflow that
runs on Beelink therefore takes two steps: change the allowlist in a pull
request, then reinstall on the host from a checkout of `main`:

```bash
sudo scripts/install-runner-guard.sh enforce
sudo systemctl restart actions.runner.ColinCee-homelab.beelink.service
```

Restart only when no deploy is running. Decisions go to the journal:
`journalctl -t homelab-runner-guard`. `observe` mode logs without refusing and
is for rollout only.

Limits: the guard stops a disallowed job before any of its steps run. It does not protect
against a compromised action used by an allowed workflow (pin actions by SHA),
and a job that has already run as `colin` could remove the hook, because the
runner can control the host.

## Manual deployment

On Beelink, select the intended checkout yourself, then:

```bash
cd /home/colin/code/homelab
scripts/deploy.sh                              # Discover and deploy all stacks
scripts/deploy.sh observability                 # Deploy one stack
scripts/deploy.sh observability backup          # Deploy selected stacks
scripts/deploy.sh --readiness-timeout 180 observability
```

The command validates every selected Compose file, secret file and systemd pair
before building or starting anything. It reads a stack's tracked `settings.env`, if any,
and never resets Git. A stack failure does not roll back earlier stacks; the
command attempts the remaining selections, prints an explicit partial result,
and exits nonzero. To write new secret files, export the deploy workflow's render-step
secrets and run `scripts/render-secrets.sh`: each is raw text, mode `0444` in a `0700` folder (every container
user reads it, no other host user).

## Adding a stack

1. Add `stacks/<name>/compose.yaml` with pinned images, persistent volumes, and
   `restart: unless-stopped`. Bind admin ports to Tailscale; reserve host
   networking for services that need it.
   Services that read a bind-mounted config file without reloading it should
   set `labels: {homelab.config-hash: ${STACK_CONFIG_HASH:-}}` so deploys
   recreate them when the stack's tracked files change.
   Set `init: true` when the main process does not handle SIGTERM (a shell
   script, a plain Python loop): as PID 1 it ignores it, so every stop waits
   10 s and then kills it.
2. If needed, add settings to `settings.env`, and each secret to the deploy workflow's render step (not all of
   them). A container reads one as a Compose `secrets:` file from `../../secrets/`, never from `environment:`,
   which inspect shows (`tests/test_no_secret_in_env.py`).
3. Run `mise run validate:compose`. Stack discovery is automatic; no task or
   deployment registry needs updating.

Within a stack, use Docker service names. Across stacks, join a network the other stack owns as `external: true`
(as flight-tracker joins `observability-otlp`); a bridge container cannot reach the host's Tailscale-bound ports,
since the container wall drops them ([ADR 0005](../adr/0005-container-network-wall.md)). A service that needs the
host itself uses host networking.

## Timers and dashboards

Deployment discovers user-unit pairs under
`stacks/<name>/systemd/<stack>-*.service` and `.timer`, copies them atomically,
reloads the user manager, and enables each timer. It does not remove unmanaged
units or run arbitrary stack hooks.

User timers require
`loginctl enable-linger colin` on the host to run without a login session.
They write the `*.prom` files Alloy reads into `/var/lib/homelab-metrics`,
which `ci-runner/install-host.sh` makes writable to all.

## Docker startup ordering

Published admin ports bind to the Tailscale address. Docker must wait for that
address, not merely for `tailscaled.service` to start. Otherwise containers can
fail to start or remain running without their network attachments.

Install the drop-in, and the nightly update settings in `apt/`, once on
Beelink (requires sudo):

```bash
sudo install -D -m 644 systemd/docker.service.d/tailscale.conf \
  /etc/systemd/system/docker.service.d/tailscale.conf
sudo systemctl daemon-reload
sudo install -m 644 apt/52unattended-upgrades-local /etc/apt/apt.conf.d/
sudo install -m 644 apt/docker-major.pref /etc/apt/preferences.d/
```

This affects the next Docker start; do not restart Docker just to install it.
The bounded wait fails the start if the expected address is missing rather than
publishing services on a public interface. If the server's Tailscale IP changes,
update this drop-in, the Compose bindings, `stacks/internal-names` (Caddyfile,
Corefile) and the `.internal` nameserver in Tailscale's DNS settings.

For containers already stranded without networking, recreate only affected
services with their settings and named volumes:

```bash
docker compose --env-file stacks/observability/settings.env \
  -f stacks/observability/compose.yaml up -d --force-recreate --no-deps grafana gateway
```

## Remove a service

Deleting a stack directory does not stop its containers or installed timers.
Disable its workflows/timers first, explicitly retire any old user units, then
stop and remove only confirmed containers. Preserve volumes until their data is
deliberately retired or backed up; do not use broad Docker pruning.

Remove unused credentials, network grants, and dashboards
after checking for shared use. Remove the stack and any special deployment
handling from Git last, so the configuration remains available during shutdown.
