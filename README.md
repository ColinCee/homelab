# Homelab

One Beelink mini-PC running Docker Compose on Ubuntu. Tailscale provides admin
access; Cloudflare Tunnel exposes the flight tracker.

[![CI](https://github.com/ColinCee/homelab/actions/workflows/ci.yaml/badge.svg?branch=main&event=push)](https://github.com/ColinCee/homelab/actions/workflows/ci.yaml?query=branch%3Amain)
[![Deploy](https://github.com/ColinCee/homelab/actions/workflows/deploy.yaml/badge.svg?branch=main&event=push)](https://github.com/ColinCee/homelab/actions/workflows/deploy.yaml?query=branch%3Amain)

## What it does

- Runs Home Assistant for home automation
- Hosts the [flight tracker](https://github.com/ColinCee/flight-tracker-at-home) backend behind Cloudflare Tunnel
- Collects metrics, logs and traces with Alloy into Prometheus and a log and a trace store, shown in Grafana; alerts go to a private Discord channel through Alertmanager
- Backs up nightly with encrypted restic snapshots to Cloudflare R2
- Accepts no connections from the internet: admin access is over Tailscale
  only, and the public API leaves through Cloudflare Tunnel, so filtering for
  it belongs at Cloudflare. A new tailnet device waits for approval in Tailscale, and an alert fires when an unknown container keeps running

## Services

| Stack | Purpose |
|-------|---------|
| `stacks/home-assistant/` | Home automation, with host networking for mDNS; Bluetooth is off |
| `stacks/observability/` | Prometheus, Grafana, Alertmanager, Alloy, the log and trace stores; pushes the [public stats](docs/public-stats.md) |
| `stacks/flight-tracker/` | Flight tracker image and Cloudflare Tunnel |
| `stacks/backup/` | Nightly encrypted restic backup to Cloudflare R2 |
| `stacks/internal-names/` | Short `.internal` names for the desktop and MacBook, listed in the [reaching-homelab-devices skill](.github/skills/reaching-homelab-devices/SKILL.md): a DNS server Tailscale sends `.internal` to, and a proxy |

Each month `backup-upkeep` ([`stacks/backup/systemd/`](stacks/backup/systemd)) prunes unused Docker images and
build cache and test-restores a file from backup; a failure alerts.

## Run locally

```bash
mise run check   # lint, typecheck, test, docs size; Compose validation needs Docker
```

[`mise.toml`](mise.toml) owns the check commands and tool versions. Dependency updates come from Renovate
([`renovate.json`](.github/renovate.json)).

## Docs

- [Architecture](docs/architecture.md): how changes reach the host and who can reach what.
- [Deploy and manage services](docs/runbooks/deploying-services.md): deploys, secrets, adding and removing stacks, startup recovery.
- [Backups](docs/runbooks/backups.md): what is backed up off-site, and how to restore.
- [CI runner](docs/runbooks/ci-runner.md): private-repo CI in Firecracker microVMs on the Beelink.
- [Security audit](docs/runbooks/security-audit.md): checking the Beelink for compromise.
- [Observe and troubleshoot](docs/observability.md): what watches what, alerts, traps.
- [Public stats](docs/public-stats.md): the numbers pushed to colincheung.dev's home page, and what they expose.
- [Decisions](docs/adr/) between designs; outstanding work is in [issues](https://github.com/ColinCee/homelab/issues).

Firewall rules, Tailscale ACLs and running services must be checked on the host; the repository alone does not prove
the live security posture.
