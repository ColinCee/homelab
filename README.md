# Homelab

One Beelink mini-PC running Docker Compose on Ubuntu. Tailscale provides admin
access; Cloudflare Tunnel exposes the flight tracker.

[![CI](https://github.com/ColinCee/homelab/actions/workflows/ci.yaml/badge.svg?branch=main&event=push)](https://github.com/ColinCee/homelab/actions/workflows/ci.yaml?query=branch%3Amain)
[![Deploy](https://github.com/ColinCee/homelab/actions/workflows/deploy.yaml/badge.svg?branch=main&event=push)](https://github.com/ColinCee/homelab/actions/workflows/deploy.yaml?query=branch%3Amain)

## What it does

- Runs Home Assistant and a Mosquitto MQTT broker for home automation
- Hosts the [flight tracker](https://github.com/ColinCee/flight-tracker-at-home) backend behind Cloudflare Tunnel
- Collects metrics and logs with Grafana, Prometheus, Loki, and Alloy; alerts go to Discord
- Backs up nightly with encrypted restic snapshots to Cloudflare R2
- Detects intrusions with CrowdSec, paired with the host firewall bouncer

## Stack

| Area | Tooling |
|------|---------|
| Host | Beelink mini-PC, Ubuntu, Docker Compose |
| Access | Tailscale (admin), Cloudflare Tunnel (public) |
| Observability | Grafana, Prometheus, Loki, Alloy |
| Backups | restic to Cloudflare R2 |
| Security | CrowdSec |
| Automation | GitHub Actions (self-hosted runner for deploys), Renovate |
| Tooling | mise, uv, Python, ruff, ty, shellcheck, yamllint, actionlint |

## Run locally

```bash
mise install
mise run lint
mise run typecheck
mise run test
mise run ci                 # Also requires Docker for Compose validation
```

[`mise.toml`](mise.toml) owns check commands and tool versions; uv owns the
repository's Python dependencies. Add checks there rather than
creating another runner or Git hook.

## How it works

Each `stacks/<name>/` directory is a self-contained Compose stack. Pushes to
`main` that touch stacks, scripts, or the deploy workflow run the deploy
workflow on the host, which renders `.env` files from allowed secrets and
applies the stacks with one shared command. See
[Deploy and manage services](docs/runbooks/deploying-services.md).

CI runs on GitHub-hosted runners. Renovate auto-merges non-major dependency
updates after checks and the release-age gate pass; major updates remain manual.
Renovate performs the merge itself so pending checks, including the age gate,
are not bypassed by GitHub's native auto-merge. Merged service updates deploy
automatically. Non-major versions can still break behavior, especially before
1.0. Release delays and grouping live in
[Renovate configuration](.github/renovate.json).

## Services

| Stack | Purpose |
|-------|---------|
| `stacks/home-assistant/` | Home automation, with host networking for Bluetooth/mDNS |
| `stacks/mqtt/` | Mosquitto broker for sensors |
| `stacks/observability/` | Grafana, Prometheus, Loki, and Alloy |
| `stacks/crowdsec/` | Intrusion detection, paired with the host firewall bouncer |
| `stacks/flight-tracker/` | Flight tracker image and Cloudflare Tunnel |
| `stacks/backup/` | Nightly encrypted restic backup to Cloudflare R2 |

The **Server health** workflow posts a weekly report (backups, disk, containers, site uptime,
pending updates) to its job summary. On the first Monday of each month it also
prunes unused Docker images and test-restores a file from backup. The **Deploy
watchdog** workflow runs off-host every 15 minutes and alerts Discord about
stuck deploys.

## Maintain and extend

- [Deploy and manage services](docs/runbooks/deploying-services.md): stack ownership and deployment contract, secrets, adding/removing stacks, startup recovery.
- [Backups](docs/runbooks/backups.md): what is backed up off-site, alerting, and how to restore.
- [Observe and troubleshoot](docs/observability.md): metrics, logs, dashboards, alerts.
- [GitHub issues](https://github.com/ColinCee/homelab/issues) track outstanding work; no parallel roadmap or decision log.
- Private operational records live in the workspace's `notes/areas/homelab/`.

Keep current instructions and necessary rationale together. Configuration owns
exact settings; Git history retains obsolete explanations.

Host firewall rules, Tailscale ACLs, and running services must be checked on the
host; repository configuration alone does not prove the live security posture.
