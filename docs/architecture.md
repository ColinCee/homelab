# Architecture Overview

This is the human map of the homelab. For the reasoning behind the current shape,
see [ADR-001](decisions/001-dokploy.md),
[ADR-003](decisions/003-observability.md),
[ADR-006](decisions/006-dokploy-gitops.md),
[ADR-010](decisions/010-agent-security-model.md), and
[ADR-011](decisions/011-docker-socket-for-workers.md).

## What runs on the Beelink

| Surface | What it does |
|---------|---------------|
| **Ubuntu 24.04 host** | Runs Docker, Tailscale, and the local storage/networking everything else depends on. |
| **Dokploy** | Control plane for repo-backed services and auto-deploys from GitHub. |
| **Tailscale** | Private admin plane for SSH, Dokploy, Grafana, Home Assistant, and the agent API. |
| **Cloudflare Tunnel** | Public ingress for the small set of services that should be internet-facing. |
| **`stacks/agents/`** | FastAPI dispatcher plus ephemeral worker containers for `/implement` and `/review`. |
| **`stacks/home-assistant/`** | Home Assistant, using host networking for Bluetooth and mDNS. |
| **`stacks/mqtt/`** | Mosquitto broker for Home Assistant sensors and automations. |
| **`stacks/observability/`** | Grafana, Prometheus, Loki, and Alloy for dashboards, metrics, logs, and alerts. |
| **`stacks/crowdsec/`** | CrowdSec IDS and firewall decisions. |

Public apps such as Flight Tracker sit beside these stacks as Dokploy-managed
services; this repo mainly owns the shared platform and infra services.

## Network topology

```text
Internet
  ├─ Cloudflare Tunnel ──> public services
  └─ Tailscale ──────────> admin access (SSH, Dokploy, Grafana, HA, agent API)

Beelink host
  ├─ Dokploy-managed services
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

For Dokploy-managed services, the happy path is:

```bash
git push origin main
```

Dokploy watches the connected GitHub repo, pulls the new commit, and runs the
service's compose deployment on the Beelink. Secrets stay in Dokploy's UI env
vars while compose files declare the required variable names
([ADR-006](decisions/006-dokploy-gitops.md)).

For stack-specific setup, manual deploy commands, and the split between
Dokploy-managed services and local compose stacks, use
[docs/runbooks/deploying-services.md](runbooks/deploying-services.md).
