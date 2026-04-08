# Copilot Instructions

Single-node homelab on a Beelink mini-PC (Ubuntu 24.04, 16 GB RAM) running containerised services behind Tailscale (admin) and Cloudflare Tunnel (public ingress), managed by Dokploy.

## Design Philosophy

- **A Philosophy of Software Design** — deep modules, narrow interfaces
- Co-locate code by feature, not file type
- Readability is the primary concern; fewer lines of code is secondary
- Prefer simplicity over abstraction — don't add layers until they're needed
- When fixing a bug or adding a pattern, audit for the same class of issue across the codebase. See the [implement skill](.github/skills/implement/SKILL.md) for review response guidelines.

## Repo Layout

- `stacks/` — Docker Compose services (agents, home-assistant, mqtt, observability, crowdsec)
- `stacks/agents/app/` — Python agent service (FastAPI, Copilot CLI integration)
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
Agent: FastAPI on beelink:8585 — AI code review via Copilot CLI (GPT-5.4)
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
2. `homelab-review-bot[bot]` posts a structured review with inline
   comments and a verdict:
   - **APPROVE** — no issues found
   - **REQUEST_CHANGES** — blocker-severity issues found
   - **COMMENT** — suggestions only, non-blocking
3. Inline comments use severity tags:
   - 🚫 **Blocker** — bugs, security, breaking changes
   - 💡 **Suggestion** — non-blocking improvement, author decides
   - ❓ **Question** — seeks clarification, non-blocking
4. Fix legitimate findings, push new commits
5. Comment `/review` to re-trigger — the bot receives all unresolved
   review threads and checks if issues were addressed
6. Repeat until you're confident the code is ready — bot reviews are
   **advisory**, not blocking. Use your judgement: fix real issues,
   dismiss false positives
7. Merge when CI passes and you're satisfied with the code

### Important

- **Bot reviews are advisory** — they inform but don't gate merges.
  The bot may produce false positives. If you're confident a finding
  is wrong, explain why in a comment and move on.
- **No `synchronize` trigger** — pushes don't auto-review. Use `/review`.
- Stale reviews are dismissed on new pushes
- Never use `--admin` to bypass branch protection
- Fork PRs are blocked from triggering reviews

## Documenting Decisions

When a decision affects architecture, security, or becomes a repeating pattern, document it as an ADR in `docs/decisions/`. See `docs.instructions.md` for the format and criteria. Small choices belong in runbooks or inline comments.
