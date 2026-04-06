# Homelab

Infrastructure-as-code and documentation for Colin's homelab — a single Beelink mini-PC running containerised services behind Tailscale and Cloudflare Tunnel.

## Quickstart

```bash
mise install          # Install Python, uv, shellcheck, actionlint, trivy
mise run lint         # Lint everything (Python, bash, YAML, Actions)
mise run typecheck    # Type-check Python
mise run test         # Run 24 pytest tests
mise run ci           # All of the above + validate compose files
```

On the server:

```bash
mise run deploy:all          # Deploy all stacks
mise run check:health        # Health check all services
mise run check:security      # Security posture audit
mise run check:vulnerabilities  # Scan images for CVEs
mise run setup               # Bootstrap a fresh server
```

## Current Services

| Service | Purpose | Stack |
|---------|---------|-------|
| [Flight Tracker](https://github.com/colincee/flight-tracker-at-home) | Real-time aviation dashboard (FastAPI + React) | Own repo |
| Home Assistant | Home automation (Bluetooth, mDNS) | `stacks/home-assistant/` |
| MQTT (Mosquitto) | Message broker for HA sensors | `stacks/mqtt/` |
| Cloudflare Tunnel | Public ingress without open ports | Flight tracker repo |

## Hardware

- **Beelink mini-PC** — Ubuntu 24.04 LTS, ~16 GB RAM, 466 GB SSD (10% used)
- **Network** — LAN, Tailscale mesh, IPv6 via ISP (no public IPv4)

## Network Topology

```
Internet
  │
  ├─ Cloudflare Tunnel ──→ Public services (flight-tracker)
  │
  └─ Tailscale ──→ Admin access (SSH, Dokploy, Home Assistant)
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

## Security

UFW firewall active (deny all except Tailscale), fail2ban monitoring SSH, automatic security patches enabled. Full audit and hardening details in [security.md](docs/private/security.md) (encrypted — clone + `git-crypt unlock` to read).

## Documentation

All docs are plain markdown — open `docs/` as an Obsidian vault if you prefer.

- **[Requirements](docs/requirements.md)** — goals, problems, and status (the "north star")
- **[Security](docs/private/security.md)** — audit findings, hardening status, periodic checklist *(encrypted)*
- **[Network](docs/private/network.md)** — topology, interfaces, traffic monitoring plan *(encrypted)*

### Decisions (append-only ADRs)

- **[ADR-001: Dokploy](docs/decisions/001-dokploy.md)** — why Dokploy, what was considered, feature comparison
- **[ADR-002: Repo Tooling](docs/decisions/002-repo-tooling.md)** — why mise + uv + Python

### Runbooks

- **[Migration: Dokploy](docs/runbooks/migration.md)** — step-by-step migration from Dockge/Tugtainer
