# Deploying Services

How to add new services to the homelab. There are two patterns depending on where the service lives.

## Pattern 1: Dokploy Compose (external repos)

For services with their own GitHub repo (e.g., flight-tracker). Dokploy pulls the `compose.yaml` from the repo and runs `docker compose up`.

### Setup

1. **Add a `compose.yaml`** to the repo root with all production services.

2. **Create the Dokploy Compose service** via API:

```bash
# Source credentials
source <(grep DOKPLOY_API_KEY credentials.md | head -1 | sed 's/.*`\(.*\)`.*/DOKPLOY_API_KEY=\1/')

# Create compose service in the flight-tracker project
curl -sf -X POST 'http://beelink:3000/api/trpc/compose.create' \
  -H "x-api-key: $DOKPLOY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"json":{"name":"my-service","description":"What it does","environmentId":"<envId>"}}'
```

3. **Link to GitHub** — set the GitHub provider, repo, and branch:

```bash
curl -sf -X POST 'http://beelink:3000/api/trpc/compose.update' \
  -H "x-api-key: $DOKPLOY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"json":{
    "composeId":"<composeId>",
    "sourceType":"github",
    "githubId":"JrqTkAxpd2g35bKN6TqBL",
    "repository":"Owner/repo-name",
    "branch":"main",
    "composePath":"./compose.yaml"
  }}'
```

4. **Set environment variables** (if needed):

```bash
curl -sf -X POST 'http://beelink:3000/api/trpc/compose.update' \
  -H "x-api-key: $DOKPLOY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"json":{"composeId":"<composeId>","env":"KEY1=value1\nKEY2=value2"}}'
```

5. **Deploy:**

```bash
curl -sf -X POST 'http://beelink:3000/api/trpc/compose.deploy' \
  -H "x-api-key: $DOKPLOY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"json":{"composeId":"<composeId>"}}'
```

### CI Auto-Deploy

Add a GitHub Actions workflow that deploys on push to main:

```yaml
deploy-backend:
  name: Deploy via Dokploy
  if: github.event_name == 'push' && github.ref == 'refs/heads/main'
  runs-on: ubuntu-latest
  steps:
    - name: Connect to Tailscale
      uses: tailscale/github-action@v4
      with:
        oauth-client-id: ${{ secrets.TS_OAUTH_CLIENT_ID }}
        oauth-secret: ${{ secrets.TS_OAUTH_SECRET }}
        tags: tag:ci

    - name: Trigger Dokploy deploy
      run: |
        curl -sf -X POST 'http://beelink:3000/api/trpc/compose.deploy' \
          -H 'x-api-key: ${{ secrets.DOKPLOY_API_KEY }}' \
          -H 'Content-Type: application/json' \
          -d '{"json":{"composeId":"<composeId>"}}'
```

**Required GitHub Secrets:** `TS_OAUTH_CLIENT_ID`, `TS_OAUTH_SECRET`, `DOKPLOY_API_KEY`

### Gotchas

- **Cloudflared entrypoint:** The `cloudflare/cloudflared` image has `ENTRYPOINT ["cloudflared", "--no-autoupdate"]`. Your compose `command` must NOT include `cloudflared` — just `tunnel run --token ...`.
- **Branch must be `main`:** If you deploy from a feature branch and delete it after merge, auto-deploy breaks.
- **Compose profiles:** Use `COMPOSE_PROFILES=prod` env var in Dokploy if your compose uses profiles.

## Pattern 2: Local Stack (homelab repo)

For infrastructure services managed in this repo (e.g., Home Assistant, MQTT, observability). Compose files live in `stacks/<service>/compose.yaml`.

### Setup

1. **Create the stack directory:**

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
      - "${TAILSCALE_IP:?}:8080:8080"
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

4. **Add to `deploy:all` depends:**

```toml
[tasks."deploy:all"]
depends = ["deploy:ha", "deploy:mqtt", "deploy:my-service"]
```

5. **Deploy:**

```bash
mise run deploy:my-service
```

### Conventions

- **Port binding:** Use `${TAILSCALE_IP:?}:hostPort:containerPort` to bind only to Tailscale. Fails fast if `TAILSCALE_IP` is not set.
- **Host network:** Only use `network_mode: host` when required (e.g., Home Assistant needs Bluetooth/mDNS).
- **Volumes:** Use named volumes for data persistence. Add data directories to `.gitignore` via `stacks/*/data/`.
- **Image versions:** Pin image tags (e.g., `grafana/grafana:11.5`) for Renovate to track and auto-PR updates.

## Which Pattern to Use

| Scenario | Pattern | Example |
|----------|---------|---------|
| Service has its own repo with CI | Dokploy Compose | flight-tracker |
| Infrastructure/monitoring service | Local Stack | HA, MQTT, observability |
| Third-party service, no custom code | Local Stack | Grafana, Prometheus |
| Service needs public ingress | Dokploy Compose + cloudflared | flight-tracker |
