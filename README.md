# Homelab

[![CI](https://github.com/ColinCee/homelab/actions/workflows/ci.yaml/badge.svg)](https://github.com/ColinCee/homelab/actions/workflows/ci.yaml)

Infrastructure-as-code and documentation for Colin's homelab — a single Beelink mini-PC running containerised services behind Tailscale and Cloudflare Tunnel, deployed via GitHub Actions.

## Quickstart

```bash
mise install          # Install Python, uv, shellcheck, actionlint, trivy
mise run lint         # Lint everything (Python, bash, YAML, Actions)
mise run typecheck    # Type-check Python
mise run test         # Run pytest
mise run ci           # All of the above + validate compose files
```

On the server:

```bash
mise run deploy:all          # Deploy all stacks
mise run check:health        # Health check all services
mise run check:vulnerabilities  # Scan images for CVEs
```

## Current Services

| Service | Purpose | Managed By |
|---------|---------|------------|
| [Flight Tracker](https://github.com/colincee/flight-tracker-at-home) | Real-time aviation dashboard (FastAPI + React) | Docker Compose (`stacks/flight-tracker/`) |
| [Cloudflared](https://github.com/cloudflare/cloudflared) | Public ingress via Cloudflare Tunnel | Docker Compose (`stacks/flight-tracker/`) |
| Home Assistant | Home automation (Bluetooth, mDNS) | Docker Compose (`stacks/home-assistant/`) |
| MQTT (Mosquitto) | Message broker for HA sensors | Docker Compose (`stacks/mqtt/`) |
| Grafana | Dashboards — metrics, logs, alerts | Docker Compose (`stacks/observability/`) |
| Prometheus | Metrics storage (30d retention) | Docker Compose (`stacks/observability/`) |
| Loki | Log aggregation (30d retention) | Docker Compose (`stacks/observability/`) |
| Grafana Alloy | Unified collector (host + container metrics/logs) | Docker Compose (`stacks/observability/`) |
| CrowdSec | Collaborative IDS + firewall bouncer | Docker Compose (`stacks/crowdsec/`) |
| Homelab Agent | AI review + issue implementation via Copilot CLI (GPT-5.4) | Docker Compose (`stacks/agents/`) |

## Hardware

- **Beelink mini-PC** — Ubuntu 24.04 LTS, ~16 GB RAM, 466 GB SSD (10% used)
- **Network** — LAN, Tailscale mesh, IPv6 via ISP (no public IPv4)

## Network Topology

```
Internet
  │
  ├─ Cloudflare Tunnel ──→ Public services (flight-tracker API)
  │
  └─ Tailscale ──→ Admin access (SSH, Home Assistant, Grafana)
         │
         └─ ACLs: desktop=full, mobile=HA only
```

## Deploy Flow

```
Push to main
  → Self-hosted runner on beelink detects changed stacks
  → Generates .env from GitHub secrets + .env.example templates
  → docker compose up
```

## Tooling

| Tool | Purpose |
|------|---------|
| [mise](https://mise.jdx.dev) | Task runner + tool version manager |
| [uv](https://docs.astral.sh/uv) | Python package management |
| [ruff](https://docs.astral.sh/ruff) | Python lint + format |
| [ty](https://docs.astral.sh/ty) | Python type checking |
| [pydantic](https://docs.pydantic.dev) | Structured models for audits |
| [trivy](https://trivy.dev) | Docker image CVE scanning |
| [Renovate](https://docs.renovatebot.com) | Automated dependency PRs |

## Observability

```
Grafana (:3001) ← Prometheus (metrics) ← Alloy (host + container scraping)
                ← Loki (logs)          ← Alloy (Docker log collection)
                                        ← CrowdSec (security metrics)
```

- **Dashboards:** Container Overview, Security (pre-provisioned)
- **Alerting:** CPU >80%, RAM >90%, Disk >80% → private Discord channel
- **Security:** CrowdSec IDS with collaborative threat intel + UFW firewall bouncer
- **External:** Healthchecks.io heartbeat (alerts on full server outage)

## Security

UFW firewall active (deny all except Tailscale), CrowdSec IDS with collaborative threat intel and UFW firewall bouncer, automatic security patches enabled, Tailscale ACLs enforcing least-privilege access. Full audit and hardening details in [security.md](docs/private/security.md) (encrypted — clone + `git-crypt unlock` to read).

## Documentation

All docs are plain markdown — open `docs/` as an Obsidian vault if you prefer.

Documentation ownership rules and refresh triggers live in
[ADR-008](docs/decisions/008-documentation-ownership.md).

| Surface | Purpose |
|---------|---------|
| `README.md` | Repo map, operator quickstart, and index into deeper docs |
| `docs/roadmap.md` | Active work, planned items, and current limitations |
| `docs/decisions/` | Accepted architecture, security, and repeating-pattern decisions |
| `docs/runbooks/` | Copy-pasteable operational procedures |
| `docs/private/` | Encrypted sensitive docs (security posture, topology, credentials-adjacent ops) |
| `.github/` | Agent-facing authoring guide, instructions, and skills |

- **[Roadmap](docs/roadmap.md)** — active work, planned items, and known limitations
- **[Architecture Overview](docs/architecture.md)** — top-level map of the Beelink, stacks, networking, and deploy path
- **[Agent Lifecycle](docs/agent-lifecycle.md)** — end-to-end `/implement` and `/review` flow, trust model, and worker ownership
- **[Observability](docs/observability.md)** — where logs, metrics, dashboards, and alerts live when things break
- **[Security](docs/private/security.md)** — audit findings, hardening status, periodic checklist *(encrypted)*
- **[Network](docs/private/network.md)** — topology, interfaces, traffic monitoring plan *(encrypted)*
- **[Copilot Authoring Guide](.github/AUTHORING.md)** — how agent-facing `.github` docs are organized

### [Decisions](docs/decisions/) (append-only ADRs)

Browse [`docs/decisions/`](docs/decisions/) for the full list. ADRs are numbered sequentially — each captures an architecture, security, or repeating-pattern decision with tradeoffs and alternatives considered.

### [Runbooks](docs/runbooks/)

Browse [`docs/runbooks/`](docs/runbooks/) for operational procedures — deploying services, operating the agent stack, and migration references.
