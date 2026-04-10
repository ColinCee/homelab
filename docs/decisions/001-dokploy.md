# ADR-001: Dokploy as Self-Hosted PaaS

**Date:** 2026-04-06
**Status:** Accepted

## Context

The homelab runs services via raw Docker Compose files scattered across directories with no centralised management, logging, metrics, alerting, or deploy pipeline (see [roadmap](../roadmap.md)).

We need a lightweight self-hosted PaaS that provides a dashboard, webhook deploys, built-in observability, and runs comfortably on a single Beelink mini-PC alongside actual services.

## Options Considered

### Coolify

- **Pros:** Largest community (~52k GitHub stars), 280+ one-click app templates, official Cloudflare Tunnel docs, mature feature set, git-based deploys
- **Cons:** Written in PHP (Laravel) — can't easily debug; 500–700 MB RAM + 5–6% CPU idle overhead; [11 critical CVEs disclosed Jan 2026](https://thehackernews.com/2026/01/coolify-discloses-11-critical-flaws.html) including CVSS 10.0 command injection and SSH key leakage; heavier than needed for a single-node homelab
- **Verdict:** Great tool, but the security track record and resource overhead don't suit our setup

### Dokploy

- **Pros:** TypeScript codebase (we can read and debug it); 350 MB RAM + 0.8% CPU idle; built-in per-container metrics and alerting; official Cloudflare Tunnel guide; GitHub App integration with auto-deploy webhooks; GHCR registry support; clean modern UI; 32k GitHub stars, fast-growing community
- **Cons:** Younger project (since 2024), fewer one-click templates (~20 vs 280), smaller community than Coolify
- **Verdict:** Best fit

### CapRover

- **Pros:** Mature, stable, very low overhead (~300–400 MB RAM), 50+ one-click apps
- **Cons:** Dated UI, slower development pace, less modern developer experience
- **Verdict:** Solid but stagnant; Dokploy is the modern successor

### Kamal

- **Pros:** Minimal overhead (CLI only), SSH-based deploys, great for Rails shops
- **Cons:** No web UI, Ruby-oriented, manual setup for everything
- **Verdict:** Wrong tool — we want a dashboard, not more CLI

### Plain Docker Compose + Lazydocker + Grafana Stack

- **Pros:** No new platform to learn, full control, minimal overhead
- **Cons:** Still manual compose management, need to build observability from scratch, no deploy pipeline, no central UI for service management
- **Verdict:** Where we are now, and it's not enough

## Feature Comparison

| Feature | Dokploy | Coolify | Current Setup |
|---------|---------|---------|---------------|
| Central dashboard | ✅ | ✅ | ❌ (Dockge, limited) |
| Git push → auto deploy | ✅ (webhook) | ✅ (webhook) | ❌ (Tugtainer polls) |
| Per-container logs | ✅ (built-in) | ✅ (via Grafana) | ❌ |
| CPU/RAM metrics | ✅ (built-in) | ✅ (via Grafana) | ❌ |
| Alerts → Discord | ✅ (built-in) | ✅ (via Grafana) | ❌ |
| GHCR registry support | ✅ | ✅ | ✅ (manual pull) |
| Cloudflare Tunnel | ✅ (documented) | ✅ (documented) | ✅ |
| Tailscale compatibility | ✅ (works out of box) | 🟡 (manual config) | ✅ |
| Idle RAM | ~350 MB | ~500–700 MB | ~0 (no platform) |
| Idle CPU | ~0.8% | ~5–6% | ~0 |
| Debuggable codebase | ✅ (TypeScript) | ❌ (PHP/Laravel) | N/A |

## Decision

**Dokploy** — lightweight, TypeScript (debuggable), covers all must-have requirements, and has a clear scaling path via Docker Swarm for multi-node.

## What It Replaces

| Current Tool | Replaced By | Benefit |
|---|---|---|
| Dockge | Dokploy web UI | No compose file location constraints |
| Tugtainer | Dokploy auto-deploy (GitHub webhook) | Instant deploys, no polling/cron |
| Manual `docker compose` | Dokploy UI + git push | Centralised management |
| *(nothing)* | Dokploy built-in logs | Per-container log viewer |
| *(nothing)* | Dokploy built-in metrics | CPU/RAM/disk per container |
| *(nothing)* | Dokploy built-in alerts | Threshold alerts → Discord |

## References

- [Dokploy docs](https://docs.dokploy.com/)
- [Dokploy GitHub](https://github.com/Dokploy/dokploy)
- [Dokploy + Cloudflare Tunnel guide](https://docs.dokploy.com/docs/core/guides/cloudflare-tunnels)
- [Dokploy + GHCR registry](https://docs.dokploy.com/docs/core/registry/ghcr)
- [Dokploy Deployment GitHub Action](https://github.com/marketplace/actions/dokploy-deployment)
- [Dokploy monitoring docs](https://docs.dokploy.com/docs/core/monitoring)
- [One-command observability stack for Dokploy](https://dev.to/quochuydev/how-i-built-a-one-command-observability-stack-for-dokploy-4ak0)
- [Securing PaaS with Tailscale + Cloudflare](https://ben.cates.fm/securing-coolify-with-tailscale-ufw-cloudflare/)
- [Dokploy vs Coolify vs CapRover comparison](https://massivegrid.com/blog/dokploy-vs-coolify-vs-caprover/)
