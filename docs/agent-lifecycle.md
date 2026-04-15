# Agent Lifecycle

This page explains how the AI agent moves through the system. For exact request
models, prompt templates, and GitHub payload handling, follow the source files
linked here instead of treating this doc as a protocol spec.

## `/implement` end to end

1. A trusted human adds the `agent` label or comments `/implement`; the trigger
   contract lives in `.github/workflows/implement.yaml`.
2. The workflow creates a repo-scoped GitHub App token, joins the tailnet, and
   POSTs to `POST /implement` on the Beelink agent API.
3. `stacks/agents/app/main.py` validates the triggering actor, then starts
   `worker-implement-<issue>` via `services/docker.py`.
4. The worker mounts the shared `repo-cache:/repo.git` and `reviews:/reviews`
   volumes, posts or updates the progress comment from `worker.py`, and calls
   `implement/orchestrator.py`.
5. The implement orchestrator validates that prompt content is trusted
   (`trust.py`), creates the `agent/issue-<N>` worktree (`services/git.py`),
   and launches Copilot CLI through `services/copilot.py`.
6. The CLI owns the repo work: edit files, commit, push, create the draft PR,
   wait for checks, mark ready, and merge. The lifecycle contract lives in
   `.github/skills/bot-implement/SKILL.md`.
7. The worker prints a JSON result; the API monitor in `main.py` records
   Prometheus metrics, removes the stopped worker container, and if the run
   produced a PR, comments `/review` on that PR.
8. That `/review` comment starts the independent advisory review flow in a fresh
   worker session.

## `/review` end to end

1. A trusted human, or the implement monitor, comments `/review` on a PR; the
   workflow contract lives in `.github/workflows/code-review.yaml`.
2. The workflow mints a GitHub App token, joins Tailscale, and POSTs to
   `POST /review`.
3. `main.py` validates the triggering actor and spawns `worker-review-<pr>`.
4. `worker.py` posts a progress comment and calls `review/orchestrator.py`.
5. The review orchestrator fetches the PR, rejects forks, loads trusted linked
   issues, creates a review worktree in `/reviews/pr-<N>`, and launches Copilot
   CLI.
6. The CLI owns the review itself: it reads the diff and posts the PR review via
   GitHub. The review contract lives in `.github/skills/bot-review/SKILL.md`.

## Trust model

Trust validation is intentionally split across layers:

| Layer | Source of truth |
|------|------------------|
| Workflow trigger gate | `.github/workflows/implement.yaml` and `.github/workflows/code-review.yaml` |
| Actor allowlist for API requests | `stacks/agents/app/trust.py` |
| Content trust for issue and PR text injected into prompts | `stacks/agents/app/trust.py`, `implement/orchestrator.py`, `review/orchestrator.py` |
| Fork PR rejection for review | `.github/workflows/code-review.yaml` and `review/orchestrator.py` |

Do not copy the actor list into docs; `trust.py` is the single source of truth.

## Worker containers

Workers are short-lived containers created from the same image as the API
container (`services/docker.py`, `stacks/agents/compose.yaml`).

- **Naming:** `worker-implement-<N>` and `worker-review-<N>`
- **Shared state:** `repo-cache:/repo.git` for the bare clone, `reviews:/reviews`
  for worktrees and CLI transcripts
- **Lifetime:** started per task, monitored by the API, and removed after exit;
  startup cleanup also reaps orphaned stopped workers

## What the orchestrator owns vs what the CLI owns

| Surface | Owns |
|---------|------|
| GitHub Actions workflows | Trigger parsing, GitHub App token minting, Tailscale connectivity, dispatch to the Beelink API |
| FastAPI API + orchestrators | Trust checks, worker spawn/monitoring, worktree setup, metrics, progress plumbing, post-implement `/review` trigger |
| Copilot CLI | File edits, git operations, PR lifecycle, and the review comment itself |

That split is the current contract from
[ADR-010](decisions/010-agent-security-model.md) and
[ADR-011](decisions/011-docker-socket-for-workers.md).
