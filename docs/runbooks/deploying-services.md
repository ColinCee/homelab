# Deploying Services

How to add and deploy services on the homelab.

## How Deploys Work

Push to `main` triggers the deploy workflow (`.github/workflows/deploy.yaml`):

```
push to main → detect changed stacks → generate .env → docker compose up
```

The workflow runs on a self-hosted runner on beelink ([ADR-012](../decisions/012-deploy-pipeline.md)).
Manual deploys are available via `workflow_dispatch` in the GitHub Actions UI.

For local/manual deploys on the server:

```bash
mise run deploy:agents       # Deploy one stack
mise run deploy:all          # Deploy everything
```

## Adding a New Stack

1. **Create the stack directory** with a `compose.yaml`:

```bash
mkdir -p stacks/my-service
```

2. **Write `compose.yaml`:**

```yaml
services:
  my-service:
    image: some-image:latest
    restart: unless-stopped
    ports:
      - "100.100.146.119:8080:8080"
    volumes:
      - my-data:/data

volumes:
  my-data:
```

3. **Add a mise deploy task** in `mise.toml`:

```toml
[tasks."deploy:my-service"]
description = "Deploy my-service"
run = "docker compose -f stacks/my-service/compose.yaml up -d"
```

4. **Add to `deploy:all` depends** in `mise.toml`.

5. **If the stack needs secrets**, create `stacks/my-service/.env.example`:

```bash
MY_SECRET=${MY_SECRET}
```

Then add `MY_SECRET` to the deploy workflow's env block and as a GitHub secret.

## Conventions

- **Port binding:** `100.100.146.119:hostPort:containerPort` — binds to Tailscale only (CGNAT, only routable within tailnet)
- **Host network:** Only `network_mode: host` when required (e.g., Home Assistant needs Bluetooth/mDNS)
- **Volumes:** Named volumes for persistence. Data directories gitignored via `stacks/*/data/`
- **Image versions:** Pin tags (e.g., `grafana/grafana:11.5`) for Renovate to track and auto-PR updates
- **Cross-stack networking:** Use the Tailscale IP (`100.100.146.119`) or host-mapped ports — containers in different compose stacks can't resolve each other via Docker DNS

## Hosting External Services

For services whose source lives in a different repo (e.g., flight-tracker),
the image is built in that repo's CI and pulled by beelink via a systemd timer.
See [ADR-013](../decisions/013-external-service-hosting.md) for the full pattern.

1. Create `stacks/<name>/compose.yaml` referencing the GHCR image
2. Create `stacks/<name>/<name>-poll.service` and `<name>-poll.timer`
   (copy from `stacks/flight-tracker/` as a template)
3. Add a case in `scripts/deploy.sh` to pull + install the timer
4. In the external repo, add a GHCR build+push job to CI
