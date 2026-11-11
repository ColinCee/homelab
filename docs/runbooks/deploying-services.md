# Deploying services

The [deploy workflow](../../.github/workflows/deploy.yaml) runs on Beelink for
stack, script, or workflow changes pushed to protected `main`. Actions resets
the server checkout to the triggering commit, renders `.env` files from an
explicit secret allowlist, and invokes the shared deployment command for the
stacks whose files changed since the last full deploy. A change to `scripts/`
or the deploy workflow, or an unknown last deploy, deploys every stack. The
last fully deployed commit is kept in `~/.local/state/homelab/last-deployed` and
moves only when every selected stack deploys, so a failed deploy is retried next
time. Stacks deploy in name order, except that the push's own changed stacks go
first, each after any stack that creates a network or volume it uses as
external (read from `docker compose config`), so a fresh host still works. A changed GitHub secret reaches a stack only when that stack next deploys:
run `scripts/deploy.sh <stack>` by hand, or delete the marker so the next push
deploys everything. Compose leaves unchanged containers running. CI and deployment are separate workflows;
require CI on pull requests before merging. A pull request that only moves
`image:` pins to new `name:tag@sha256:` digests in existing `compose.yaml` files
gets only Compose validation of the stacks it touches in the `check` job
([`pin_only.py`](../../scripts/pin_only.py)); every other change runs `mise run check`.

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
Keep PR CI off the deploy runner: public repositories' CI on GitHub-hosted
runners, private repositories' CI only in the
[Firecracker VMs](ci-runner.md), which hold no secrets, deploy access or home
network. Keep Beelink deployment restricted to trusted `main`. After checks, Renovate auto-merges eligible dependency PRs; other changes are
human-directed.
Admins can bypass the ruleset, including via agents using their credentials.
Renovate does not have that bypass. Workflow conditions alone are
not access controls.

This avoids giving hosted CI tailnet access or maintaining another deployment
platform. Admin ports stay bound to Tailscale. Secrets are explicitly passed
to Compose, never sourced as shell code.

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
   Services that read a bind-mounted config file without reloading it should
   set `labels: {homelab.config-hash: ${STACK_CONFIG_HASH:-}}` so deploys
   recreate them when the stack's tracked files change.
   Set `init: true` when the main process does not handle SIGTERM (a shell
   script, a plain Python loop): as PID 1 it ignores it, so every stop waits
   10 s and then kills it.
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

For externally built services, CI in the source repo publishes to GHCR and the
image is pinned by digest, so a new image needs the digest updated in Git (by
Renovate).

Do not copy the image-polling pattern for new services. User timers require
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
services with their existing `.env` and named volumes:

```bash
docker compose --env-file stacks/observability/.env \
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
