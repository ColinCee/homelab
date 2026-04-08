---
name: code-review
description: Perform a structured PR code review for this homelab repo. Use when asked to review a pull request, diff, or set of changes.
---

# Code Review Skill

You are reviewing changes in a single-node homelab repo. The repo manages Docker Compose stacks, a FastAPI agent service, GitHub Actions workflows, and documentation.

## Review Focus

Focus on issues that actually matter. Do NOT comment on style or formatting — ruff and yamllint handle that.

Review for:
- **Bugs** — logic errors, race conditions, missing error handling
- **Security** — exposed secrets, missing auth, privilege escalation, injection
- **Breaking changes** — API contract changes, config renames, removed features
- **Operational risk** — resource leaks, missing healthchecks, unbounded growth

## Network Restrictions

The review agent container runs behind a Squid egress proxy. Only these domains are reachable:

### Core (GitHub + Copilot API)
- `github.com`, `api.github.com`
- `api.githubcopilot.com`
- `githubusercontent.com`

### Documentation
- `docs.python.org`
- `docs.docker.com`
- `fastapi.tiangolo.com`
- `docs.pydantic.dev`
- `grafana.com`
- `prometheus.io`
- `tailscale.com`
- `developers.cloudflare.com`
- `docs.github.com`
- `stackoverflow.com`

All other outbound requests will be blocked. Do not attempt to fetch URLs outside this list.

## Stack Architecture

```
Internet
  ├─ Cloudflare Tunnel → Public services
  └─ Tailscale → Admin access (SSH, Dokploy, HA, Grafana)
```

| Stack | Key Details |
|-------|------------|
| agents | FastAPI review agent, Squid proxy sidecar, read-only container |
| home-assistant | Host network (Bluetooth/mDNS) |
| mqtt | Mosquitto broker, port 1883 |
| observability | Grafana, Prometheus, Loki, Alloy |
| crowdsec | IDS + UFW firewall bouncer |

## Compose Conventions

- Port binding: `${TAILSCALE_IP:?}:hostPort:containerPort`
- Restart: `unless-stopped` on all services
- Images: Pin versions for Renovate tracking
- Volumes: Named volumes for persistence, `stacks/*/data/` gitignored

## Python Conventions

- Python 3.12, FastAPI, httpx, Pydantic
- Lint: ruff. Types: ty. Tests: pytest
- Mock external calls in tests — never hit real APIs
- Agent returns structured JSON — never writes to GitHub directly

## Output Format

Return your review as a single raw JSON object (no code fences) with this structure:

```json
{
  "summary": "Brief overall assessment",
  "verdict": "approve" | "request_changes",
  "comments": [
    {
      "path": "path/to/file",
      "line": 42,
      "severity": "blocker" | "suggestion" | "question",
      "body": "What is wrong and why",
      "start_line": null
    }
  ]
}
```

Rules:
- `verdict` is `request_changes` ONLY if there is at least one `blocker` comment
- `line` is the line number in the **current version** of the file
- `start_line` is optional — set it for multi-line ranges, otherwise `null`
- Keep comments concise — state WHAT is wrong and WHY
- If the code looks good, return `approve` with an empty comments array
