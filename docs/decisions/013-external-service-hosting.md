# ADR-013: External service hosting — GHCR image polling

**Date:** 2026-04-18
**Status:** Accepted

## Context

With Dokploy decommissioned (ADR-012), services whose source lives in other
repos need a way to build, publish, and deploy to beelink. The first service
needing this is `flight-tracker-at-home` — a Python backend whose source lives
in `ColinCee/flight-tracker-at-home` but runs on beelink alongside the homelab
stacks.

Previously, Dokploy handled build + deploy in one step by pulling the source
repo and running `docker compose build` on the server. The replacement needs to:

1. Build images in CI (not on beelink — it's a 16 GB mini-PC)
2. Deliver new images to beelink automatically
3. Be fully GitOps — no manual steps, no standing credentials between repos

## Options Considered

### A: Cross-repo workflow dispatch

External repo CI builds the image, then triggers homelab's `deploy.yaml` via
`workflow_dispatch` API to pull and restart.

**Pros:**
- Deploy is immediate (no polling delay)
- Uses existing homelab deploy pipeline

**Cons:**
- Requires a cross-repo token (GitHub App or PAT) stored in the external repo
- Couples the external repo to homelab's workflow interface
- Standing credential to manage and rotate

**Verdict: Rejected.** Adds credential management complexity for marginal
latency improvement over polling.

### B: Watchtower / Tugtainer / WUD

Container sidecar that polls registries and auto-updates running containers.

**Pros:**
- Well-known pattern, battle-tested
- Container-native (runs alongside the stack)

**Cons:**
- Watchtower is archived/deprecated
- Alternatives (Tugtainer, WUD) are full dashboard apps — overkill for
  watching one image
- Requires Docker socket mount
- Another container to maintain and update

**Verdict: Rejected.** Over-engineered for a single-image use case. Adds a
dependency we'd need to track and update.

### C: Systemd timer polling GHCR

A user-level systemd timer runs every 30s, executing `docker compose pull` +
`docker compose up -d`. If the image digest hasn't changed, both commands are
no-ops.

**Pros:**
- Zero dependencies — uses only Docker and systemd (already on beelink)
- Timer + service unit files live in the repo (GitOps)
- `deploy.sh` installs them automatically on deploy
- No Docker socket mount beyond what compose already needs
- No cross-repo credentials
- Idempotent — safe to run every 30s

**Cons:**
- Up to 30s delay between image push and deploy
- Polls even when nothing has changed (negligible — one HEAD request to GHCR)

**Verdict: Recommended.** Simplest option with no new dependencies. 30s delay
is acceptable for a homelab.

## Decision

**Option C: Systemd timer polling GHCR.**

The pattern for hosting external services on beelink:

1. **External repo CI** builds and pushes image to GHCR on merge to main
   (uses `GITHUB_TOKEN` with `packages:write` — no extra secrets)
2. **Homelab stack** (`stacks/<name>/compose.yaml`) references the GHCR image
3. **Systemd timer** (`stacks/<name>/<name>-poll.timer` + `.service`) polls
   every 30s, runs `docker compose pull` + `up -d`
4. **`deploy.sh`** installs the timer units to `~/.config/systemd/user/` on
   first deploy (user-level systemd, no sudo needed)

### Adding a new external service

1. Create `stacks/<name>/compose.yaml` referencing the GHCR image
2. Create `stacks/<name>/<name>-poll.service` and `<name>-poll.timer`
3. Add a case in `deploy.sh` to pull + install the timer
4. In the external repo, add a GHCR build+push job to CI

### Implementation details

- Timer uses `OnUnitActiveSec=30s` (runs 30s after last completion)
- Service runs as user `colin` (already in docker group)
- `loginctl enable-linger colin` ensures user timers run without a login session
- `deploy.sh` exports `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` so
  `systemctl --user` works from the GitHub Actions runner context

## References

- `stacks/flight-tracker/compose.yaml` — first service using this pattern
- `stacks/flight-tracker/flight-tracker-poll.service` — poll service unit
- `stacks/flight-tracker/flight-tracker-poll.timer` — poll timer unit
- `scripts/deploy.sh` — timer installation logic
- ADR-012: Deploy pipeline (self-hosted runner)
- `ColinCee/flight-tracker-at-home/.github/workflows/deploy.yml` — GHCR build job
