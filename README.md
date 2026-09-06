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
git config core.hooksPath .githooks  # Guard against plaintext private-doc commits
```

CI runs on GitHub-hosted runners. Dependency updates arrive as Renovate PRs;
merging is a human decision. There is no issue-implementation or review service.

## Deployment

Changes to stacks, scripts, or the deploy workflow on `main` reconcile all stacks
on Beelink. The workflow checks out the triggering commit, renders stack secrets,
and runs `scripts/deploy.sh`. Unchanged containers are left running by Compose.
Main must be protected: the deploy runner has host-level access.

On the server, to deploy the current checkout using existing stack `.env` files:

```bash
scripts/deploy.sh            # All stacks
scripts/deploy.sh knowledge  # One stack
```

This command does not fetch or reset Git. See the
[deployment runbook](docs/runbooks/deploying-services.md) for secrets, timers,
adding services, and retiring the old agent containers.

## Operations

- [Observability](docs/observability.md): metrics, logs, dashboards, alerts
- [Runbooks](docs/runbooks/): deployment and knowledge-base operations
- [Roadmap](docs/roadmap.md): known limitations and planned work
- [Decisions](docs/decisions/): rationale worth retaining, not a required process
- `docs/private/`: git-crypt-encrypted operational records; never commit plaintext

Host firewall rules, Tailscale ACLs, and running services must be checked on the
host; repository configuration alone does not prove the live security posture.
