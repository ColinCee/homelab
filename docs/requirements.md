# Requirements

Living document — the "north star" for what this homelab should do and why. Every [decision](decisions/) traces back to a requirement here.

## Problems (what we're solving)

| ID | Problem | Status |
|----|---------|--------|
| P1 | No centralised management — `cd` into each project, `docker compose up/down` manually | ✅ Solved — [Dokploy](decisions/001-dokploy.md) dashboard on port 3000 |
| P2 | No logging — container stdout disappears; no way to search or retain logs | ✅ Solved — Dokploy built-in per-container logs |
| P3 | No metrics — no visibility into CPU, RAM, or disk usage per container | ✅ Solved — Dokploy built-in metrics |
| P4 | No alerting — if something crashes at 3am, nobody knows | ✅ Solved — Dokploy → Discord alerts (build errors, deploys, threshold) |
| P5 | No clean deploy pipeline — Tugtainer polls GHCR on 2-min cron with stagger hacks | ✅ Solved — CI → Tailscale → Dokploy API deploy |
| P6 | Dockge friction — requires compose files in `/opt/stacks/`, doesn't work with `~/code/` | ✅ Solved — Dockge removed, stacks in repo |
| P7 | No security visibility — no firewall, no intrusion detection, no audit trail | ✅ Solved — CrowdSec IDS + Grafana security dashboard + UFW bouncer |
| P8 | No network traffic monitoring — can't see what's hitting services or from where | ✅ Solved — Grafana + Prometheus + Loki + Alloy |

## Requirements

### Service Management

| ID | Requirement | Priority | Status |
|----|-------------|----------|--------|
| R1 | Single dashboard for all services, status, logs, resource usage | Must | ✅ Dokploy |
| R2 | Git push → webhook → deploy (not polling) | Must | ✅ Tailscale CI → Dokploy API |
| R3 | Add, remove, restart services from a web UI | Must | ✅ Dokploy |
| R4 | Keep existing network topology (Tailscale admin, CF Tunnel public) | Must | ✅ |

### Observability

| ID | Requirement | Priority | Status |
|----|-------------|----------|--------|
| R5 | Per-container real-time logs with search | Must | ✅ Dokploy built-in |
| R6 | CPU, RAM, disk metrics per container | Must | ✅ Dokploy built-in |
| R7 | Alert thresholds → Discord (e.g. CPU > 80%, RAM > 90%) | Must | ✅ Dokploy → Discord |
| R8 | Historical log retention and search | Nice | ✅ Loki (30d retention) + Grafana Explore |
| R9 | Long-term host metrics and trending | Nice | ✅ Prometheus (30d retention) + Grafana dashboards |
| R10 | Endpoint uptime monitoring + status page | Nice | ✅ Healthchecks.io (external heartbeat) |

### Security

| ID | Requirement | Priority | Status |
|----|-------------|----------|--------|
| R11 | Host firewall — deny all except Tailscale | Must | ✅ UFW enabled |
| R12 | Automatic security patches | Must | ✅ unattended-upgrades fixed |
| R13 | Brute-force protection | Must | ✅ fail2ban active |
| R14 | Intrusion detection — know if someone gains access | Must | ✅ CrowdSec (collaborative IDS) + UFW firewall bouncer |
| R15 | Network request stats — see what's hitting services | Should | ✅ Alloy → Prometheus/Loki → Grafana dashboards |
| R16 | Docker socket hardening | Should | 🔲 Planned |

### Operational

| ID | Requirement | Priority | Status |
|----|-------------|----------|--------|
| R17 | Lightweight — runs comfortably on single Beelink alongside services | Must | ✅ |
| R18 | Maintainable — can debug and understand when it breaks | Must | ✅ TypeScript codebase (Dokploy) |
| R19 | Scales to multi-node when needed without full migration | Nice | 🔲 Dokploy Swarm mode (later) |
| R20 | Private self-hosted knowledge base / notes (accessible via Tailscale only) | Nice | 🔲 Evaluate after Dokploy migration |

## Scaling Path

| Scale | Tool | When |
|-------|------|------|
| 1 node, <15 services | Dokploy (Docker) | Now |
| 2–5 nodes | Dokploy multi-server (Docker Swarm) | When a second machine is added |
| Outgrow Swarm | K3s | If/when Swarm isn't enough |
