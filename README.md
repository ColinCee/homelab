# Homelab

One Beelink mini-PC running Docker Compose on Ubuntu. Tailscale provides admin
access; Cloudflare Tunnel exposes the flight tracker.

## Services

| Stack | Purpose |
|-------|---------|
| `stacks/home-assistant/` | Home automation, with host networking for Bluetooth/mDNS |
| `stacks/mqtt/` | Mosquitto broker for sensors |
| `stacks/observability/` | Grafana, Prometheus, Loki, and Alloy |
| `stacks/crowdsec/` | Intrusion detection, paired with the host firewall bouncer |
| `stacks/knowledge/` | Postgres/pgvector and the notes search/ingest CLI |
| `stacks/flight-tracker/` | Flight tracker image and Cloudflare Tunnel |

## Development

```bash
mise install
mise run lint
mise run typecheck
mise run test
mise run ci                 # Also requires Docker for Compose validation
```

[`mise.toml`](mise.toml) owns check commands and tool versions; uv owns the
knowledge application's Python dependencies. Add checks there rather than
creating another runner or Git hook.

CI runs on GitHub-hosted runners. Renovate proposes pinned dependency updates;
passing CI is not approval to deploy. A human merges them. Release delays and
grouping live in [Renovate configuration](.github/renovate.json).

## Maintain and extend

- [Deploy and manage services](docs/runbooks/deploying-services.md): trust boundaries, secrets, adding/removing stacks, startup recovery.
- [Observe and troubleshoot](docs/observability.md): metrics, logs, dashboards, alerts.
- [Operate and extend knowledge search](docs/runbooks/knowledge-base.md): ingestion, privacy, retrieval, backups, credentials.
- [GitHub issues](https://github.com/ColinCee/homelab/issues) track outstanding work; no parallel roadmap or decision log.
- Private operational records live in the workspace's `notes/areas/homelab/`.

Keep current instructions and necessary rationale together. Configuration owns
exact settings; Git history retains obsolete explanations.

Host firewall rules, Tailscale ACLs, and running services must be checked on the
host; repository configuration alone does not prove the live security posture.
