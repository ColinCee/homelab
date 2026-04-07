# Copilot Instructions

Single-node homelab on a Beelink mini-PC (Ubuntu 24.04, 16 GB RAM) running containerised services behind Tailscale (admin) and Cloudflare Tunnel (public ingress), managed by Dokploy.

## Design Philosophy

- **A Philosophy of Software Design** — deep modules, narrow interfaces
- Co-locate code by feature, not file type
- Readability is the primary concern; fewer lines of code is secondary
- Prefer simplicity over abstraction — don't add layers until they're needed

## Repo Layout

- `stacks/` — Docker Compose services (agents, home-assistant, mqtt, observability, crowdsec)
- `stacks/agents/app/` — Python agent service (FastAPI, Copilot API integration)
- `docs/` — ADRs, runbooks, requirements (works as an Obsidian vault)
- `access.md` — local-only server credentials and API keys (never committed, gitignored)

## Toolchain

| Tool | Purpose | Command |
|------|---------|---------|
| mise | Task runner + tool versions | `mise run <task>` |
| uv | Python package management | `uv sync`, `uv run` |
| ruff | Lint + format | `uv run ruff check`, `uv run ruff format` |
| ty | Type checking | `cd stacks/agents/app && uv run ty check .` |
| pytest | Tests | `cd stacks/agents/app && uv run pytest` |
| yamllint | YAML linting | `yamllint -c .yamllint.yaml stacks/` |
| actionlint | GitHub Actions linting | `actionlint` |
| shellcheck | Shell script linting | `shellcheck .githooks/*` |

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
Agent: FastAPI on beelink:8585 — AI code review via Copilot API (GPT-5.4)
Platform: Dokploy (manages container lifecycle, auto-deploys from main)
```

## Pull Request Workflow

This repo has an automated AI code review system. Follow this process:

### Creating PRs

1. **Create PRs as drafts** — `gh pr create --draft`
2. Push commits, iterate until ready
3. Mark ready: `gh pr ready <number>` → triggers first AI review

### Review cycle

1. When a PR is opened or marked ready, `code-review.yaml` auto-triggers
2. GPT-5.4 on the Beelink agent posts a structured review as
   `github-actions[bot]` with inline comments and a verdict:
   - **APPROVE** — no issues, can merge
   - **REQUEST_CHANGES** — must-fix or security issues block merge
   - **COMMENT** — nitpicks only, doesn't block
3. Inline comments use severity tags:
   - 🔧 **Must Fix** — blocks merge
   - 💡 **Nitpick** — optional improvement
   - 🔒 **Security** — always blocks merge
4. Fix any must-fix/security findings, push new commits
5. Comment `/review` to re-trigger — the model receives its previous
   review and checks if issues were resolved
6. Repeat until APPROVE
7. Repo owner merges — **never merge PRs yourself**

### Important

- **No `synchronize` trigger** — pushes don't auto-review. Use `/review`.
- Stale reviews are dismissed on new pushes (branch protection)
- Never use `--admin` to bypass branch protection
- Fork PRs are blocked from triggering reviews
