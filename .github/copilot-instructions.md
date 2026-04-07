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

1. **Always create PRs as drafts first** — use `gh pr create --draft`
2. Work on the branch, push commits, iterate until ready
3. When ready, mark as ready for review: `gh pr ready <number>`

### Review cycle

1. When a PR is marked ready (or opened as non-draft), the `code-review.yaml`
   workflow triggers automatically
2. The AI reviewer (GPT-5.4 on the Beelink agent) posts a structured review
   as `github-actions[bot]` with:
   - **APPROVE** — no issues found, PR can be merged
   - **REQUEST_CHANGES** — must-fix or security issues found, blocks merge
   - **COMMENT** — nitpicks only, doesn't block merge
3. Reviews include inline comments on specific lines with severity tags:
   - 🔧 **Must Fix** — blocks merge
   - 💡 **Nitpick** — optional improvement
   - 🔒 **Security** — always blocks merge
4. Fix any must-fix/security issues, push new commits
5. Comment `/review` to re-trigger the AI review
6. Repeat steps 4-5 until the bot APPROVEs
7. The repo owner merges — **never merge PRs yourself**

### Branch protection

- 1 approving review required (from `github-actions[bot]`)
- CI (`check` job) must pass
- Stale reviews are dismissed on new pushes

### Security

- Fork PRs are blocked from triggering reviews (defense-in-depth)
- `/review` command restricted to OWNER/MEMBER/COLLABORATOR roles
- Never use `--admin` to bypass branch protection
