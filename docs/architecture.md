# Architecture Overview

This is the human map of the homelab. For the reasoning behind the current shape,
see [ADR-003](decisions/003-observability.md),
[ADR-010](decisions/010-agent-security-model.md),
[ADR-011](decisions/011-docker-socket-for-workers.md),
[ADR-012](decisions/012-deploy-pipeline.md), and
[ADR-013](decisions/013-external-service-hosting.md).

## What runs on the Beelink

| Surface | What it does |
|---------|---------------|
| **Ubuntu 24.04 host** | Runs Docker, Tailscale, and the local storage/networking everything else depends on. |
| **GitHub Actions runner** | Self-hosted runner that executes deploy workflows locally ([ADR-012](decisions/012-deploy-pipeline.md)). |
| **Tailscale** | Private admin plane for SSH, Grafana, Home Assistant, and the agent API. |
| **Cloudflare Tunnel** | Public ingress for the small set of services that should be internet-facing. |
| **`stacks/agents/`** | FastAPI dispatcher plus ephemeral worker containers for `/implement` and `/review`. |
| **`stacks/home-assistant/`** | Home Assistant, using host networking for Bluetooth and mDNS. |
| **`stacks/mqtt/`** | Mosquitto broker for Home Assistant sensors and automations. |
| **`stacks/observability/`** | Grafana, Prometheus, Loki, and Alloy for dashboards, metrics, logs, and alerts. |
| **`stacks/crowdsec/`** | CrowdSec IDS and firewall decisions. |
| **`stacks/knowledge/`** | Postgres 17 + pgvector for personal knowledge base semantic search ([ADR-016](decisions/016-knowledge-base.md)). |
| **`stacks/flight-tracker/`** | Flight Tracker API + Cloudflare Tunnel sidecar. |

## Network topology

```text
Internet
  ├─ Cloudflare Tunnel ──> public services (flight-tracker)
  └─ Tailscale ──────────> admin access (SSH, Grafana, HA, agent API)

Beelink host
  ├─ Self-hosted GitHub Actions runner (outbound HTTPS to GitHub only)
  └─ Docker Compose stacks from this repo
       ├─ per-stack Docker networks for local service-to-service traffic
       └─ host-mapped ports on 100.100.146.119 for cross-stack access
```

Two practical rules matter when you are tracing traffic:

1. Within one compose stack, containers can use Docker DNS normally.
2. Across stacks, use the Tailscale bind (`100.100.146.119`) or host-mapped
   ports instead of service names; `host.docker.internal` is the fallback when a
   stack needs to reach the host. For the operating pattern, see
   [docs/runbooks/deploying-services.md](runbooks/deploying-services.md).

## How deploys work

Push to `main` triggers the deploy workflow:

```text
push to main → detect changed stacks → generate .env from GitHub secrets → docker compose up
```

The workflow runs on a self-hosted runner on beelink — no SSH, no tailnet
join from CI. Secrets live in GitHub and are written to `.env` files via
`.env.example` templates at deploy time
([ADR-012](decisions/012-deploy-pipeline.md)).

### External services (GHCR image polling)

Services whose source lives in other repos (e.g., flight-tracker) follow a
different path: the external repo CI builds and pushes images to GHCR, and a
systemd timer on beelink polls for new digests every 30s
([ADR-013](decisions/013-external-service-hosting.md)).

```text
push to external repo → CI builds + pushes to GHCR → beelink timer pulls + restarts (≤30s)
```

For stack-specific setup and adding new services, see
[docs/runbooks/deploying-services.md](runbooks/deploying-services.md).
