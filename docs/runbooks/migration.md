# Migration: Dokploy

Step-by-step guide for migrating from Dockge/Tugtainer to Dokploy.

**Requirements:** R1, R2, R3, R5, R6, R7
**Decision:** [ADR-001](../decisions/001-dokploy.md)

## Pre-Migration Checklist

- [ ] Back up all compose files and `.env` files from `/opt/stacks/`
- [ ] Note current container resource usage as baseline
- [ ] Ensure Tailscale and Cloudflare Tunnel are working

## Installation

- [ ] Install Dokploy on Beelink
- [ ] Bind Dokploy dashboard to Tailscale IP only
- [ ] Verify dashboard accessible via Tailscale

## GitHub Integration

- [ ] Connect GitHub account (install Dokploy GitHub App)
- [ ] Add GHCR registry credentials

## Service Migration

### Flight Tracker Backend

```
Push to main
  → GitHub Actions builds Docker image
  → Pushes to GHCR (ghcr.io/colincee/flight-tracker-at-home/backend)
  → Calls Dokploy API (via official GitHub Action) to trigger deploy
  → Dokploy pulls new image and restarts container
```

- [ ] Create Dokploy application from GHCR image
- [ ] Configure auto-deploy webhook
- [ ] Update flight-tracker CI to use [Dokploy Deployment GitHub Action](https://github.com/marketplace/actions/dokploy-deployment) instead of Tugtainer
- [ ] Verify Cloudflare Tunnel still routes to backend

### Flight Tracker Frontend

Frontend deploys to Cloudflare Pages (unchanged) — Dokploy doesn't manage it.

### Home Assistant

- [ ] Migrate as Docker Compose service within Dokploy
- [ ] Preserve `network_mode: host` and Bluetooth capabilities (`NET_ADMIN`, `NET_RAW`)
- [ ] Verify Bluetooth and mDNS still work after migration

### MQTT (Mosquitto)

- [ ] Migrate as Docker Compose service within Dokploy
- [ ] Verify port bindings remain on Tailscale IP only

## Observability Setup

- [ ] Configure Discord webhook for alerts
- [ ] Set CPU/RAM alert thresholds (CPU > 80%, RAM > 90%)
- [ ] Verify per-container logs and metrics are visible in dashboard

## Cleanup

- [ ] Remove Dockge (`/opt/dockge/`)
- [ ] Remove Tugtainer from flight-tracker compose
- [ ] Verify all services healthy for 24 hours
- [ ] Update this doc with any issues encountered

## Rollback Plan

If Dokploy fails, the original compose files are still in `/opt/stacks/` and `~/code/`. Run `docker compose up -d` in each project directory to restore the previous setup.
