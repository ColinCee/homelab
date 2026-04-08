# Migration: Dokploy

Step-by-step guide for migrating from Dockge/Tugtainer to Dokploy.

**Requirements:** R1, R2, R3, R5, R6, R7
**Decision:** [ADR-001](../decisions/001-dokploy.md)
**Status:** ✅ Complete

## What Was Done

### Installation

- [x] Installed Dokploy manually (official script fails due to Tailscale on port 443)
- [x] Docker Swarm initialised with `--advertise-addr` on Tailscale IP
- [x] Dokploy dashboard accessible at `http://beelink:3000` (Tailscale only)
- [x] No Traefik — not needed behind Tailscale

### GitHub Integration

- [x] Connected GitHub account (Dokploy GitHub App installed)
- [x] Flight tracker builds from source (Dockerfile), not GHCR images

### Service Migration

#### Flight Tracker Backend
- [x] Created as Dokploy application (GitHub source, dockerfile build)
- [x] CORS_ORIGINS env var configured
- [x] Cloudflared deployed as separate Dokploy app (docker image source)
- [x] Cloudflare tunnel routing updated to Dokploy service name
- [x] Public endpoint verified: `https://api.colincheung.dev/docs` ✅
- [x] Auto-deploy via CI: Tailscale GitHub Action → Dokploy API (PR #50 on flight-tracker repo)

#### Flight Tracker Frontend
Frontend deploys to Cloudflare Pages (unchanged) — Dokploy doesn't manage it.

#### Home Assistant
- [x] Compose moved to `~/code/homelab/stacks/home-assistant/`
- [x] Data moved from `/opt/stacks/` to repo stacks directory
- [ ] Optional: migrate to Dokploy-managed compose service

#### MQTT (Mosquitto)
- [x] Compose moved to `~/code/homelab/stacks/mqtt/`
- [x] Data moved from `/opt/stacks/` to repo stacks directory
- [x] Port bindings use hardcoded Tailscale IP
- [ ] Optional: migrate to Dokploy-managed compose service

### Observability

- [x] Discord webhook configured for all alert types (build errors, deploys, restarts, thresholds)

### Cleanup

- [x] Dockge stopped and removed (`/opt/dockge/` deleted)
- [x] Tugtainer removed (container + volume deleted)
- [x] Old flight-tracker compose stopped
- [x] `/opt/stacks/` removed entirely
- [x] All services verified healthy

### Security Hardening (done alongside migration)

- [x] Tailscale ACLs tightened to least-privilege (desktop=full, mobile=HA only, CI=Dokploy only)
- [x] Git history squashed to remove leaked Tailscale IP
- [x] MQTT compose uses hardcoded Tailscale IP for port binding

## Final State

| Service | Managed By | How It Deploys |
|---------|-----------|----------------|
| Flight tracker backend + cloudflared | Dokploy (Compose) | CI → Tailscale → Dokploy API |
| Home Assistant | Docker Compose (repo) | `mise run deploy:all` on server |
| MQTT | Docker Compose (repo) | `mise run deploy:all` on server |
| Observability (Grafana, Prometheus, Loki, Alloy) | Docker Compose (repo) | `mise run deploy:all` on server |
| CrowdSec | Docker Compose (repo) | `mise run deploy:all` on server |
| Dokploy + Postgres + Redis | Docker Swarm | Self-managed |

## Rollback Plan

HA and MQTT compose files are in `stacks/` — run `docker compose up -d` to restore.
Flight tracker can be redeployed from GHCR by reverting the CI workflow changes.
