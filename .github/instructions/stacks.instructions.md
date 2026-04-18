---
applyTo: "stacks/**"
---

# Stack Conventions

Each stack is a self-contained directory under `stacks/<service>/` with a `compose.yaml` and any config files co-located alongside it.

## Compose Patterns

- **Port binding:** `100.100.146.119:hostPort:containerPort` — binds to Tailscale interface only (CGNAT range, only routable within tailnet)
- **Host network:** Only `network_mode: host` when required (e.g., Home Assistant needs Bluetooth/mDNS)
- **Restart policy:** `restart: unless-stopped` on all services
- **Image tags:** Pin versions (e.g., `grafana/grafana:11.5`) so Renovate can track and auto-PR updates
- **Volumes:** Named volumes for persistence. Data directories gitignored via `stacks/*/data/`
- **Cross-stack networking:** Containers in different compose stacks can't resolve each other's service names via Docker DNS. Use the Tailscale IP (`100.100.146.119`) or host-mapped ports for cross-stack references.

## Current Stacks

| Stack | Services | Notes |
|-------|----------|-------|
| home-assistant | HA (host network) | Bluetooth/mDNS needs host networking |
| mqtt | Mosquitto | Broker for HA sensors, port 1883 |
| observability | Grafana, Prometheus, Loki, Alloy | Metrics, logs, dashboards, alerting → Discord |
| crowdsec | CrowdSec engine | IDS with UFW firewall bouncer on host |
| knowledge | Postgres 17 + pgvector | Persistent vector store for the personal knowledge base |

## Validation

All compose files must pass `docker compose config --quiet`. CI validates this automatically.

```bash
mise run validate:compose   # Validate all compose files
mise run deploy:<stack>     # Deploy a specific stack
mise run deploy:all         # Deploy everything
```

## Adding a New Stack

1. `mkdir stacks/<name>` with a `compose.yaml`
2. Add `deploy:<name>` task in `mise.toml`
3. Add to `deploy:all` depends list
4. See `docs/runbooks/deploying-services.md` for full walkthrough
