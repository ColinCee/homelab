# Copilot Instructions

Single-node homelab on a Beelink mini-PC (Ubuntu 24.04, 16 GB RAM) running containerised services behind Tailscale (admin) and Cloudflare Tunnel (public ingress), managed by Dokploy.

## Design Philosophy

- **A Philosophy of Software Design** — deep modules, narrow interfaces
- Co-locate code by feature, not file type
- Readability is the primary concern; fewer lines of code is secondary
- Prefer simplicity over abstraction — don't add layers until they're needed

## Repo Layout

- `src/homelab/` — Python tooling (health checks, security audits, shared models)
- `stacks/` — Docker Compose services (home-assistant, mqtt, observability, crowdsec)
- `tests/` — pytest tests mirroring `src/`
- `docs/` — ADRs, runbooks, requirements (works as an Obsidian vault)
- `access.md` — local-only server credentials and API keys (never committed, gitignored)

## Toolchain

| Tool | Purpose | Command |
|------|---------|---------|
| mise | Task runner + tool versions | `mise run <task>` |
| uv | Python package management | `uv sync`, `uv run` |
| ruff | Lint + format | `uv run ruff check`, `uv run ruff format` |
| ty | Type checking | `uv run ty check src/` |
| pytest | Tests | `uv run pytest tests/ -v` |
| yamllint | YAML linting | `uv run yamllint -c .yamllint.yaml stacks/` |

## Key Commands

```bash
mise run lint         # Lint everything (Python, bash, YAML, Actions)
mise run typecheck    # Type-check Python
mise run test         # Run pytest
mise run ci           # All of the above + validate compose files
```

## Architecture

```
Internet
  ├─ Cloudflare Tunnel → Public services (flight-tracker)
  └─ Tailscale → Admin access (SSH, Dokploy, HA, Grafana)

Stacks: Home Assistant, MQTT, Observability (Grafana/Prometheus/Loki/Alloy), CrowdSec
Platform: Dokploy (manages external-repo services like flight-tracker)
```
