# ADR-015: Self-hosted runner security policy

**Date:** 2026-04-19
**Status:** Accepted

## Context

The Beelink serves as a self-hosted GitHub Actions runner (`runs-on: beelink`). Workflows execute directly on the machine — they have access to Docker, the filesystem, database credentials, and the Tailscale network.

GitHub Actions workflows can be triggered by external actors on public repositories. Anyone can fork a public repo, modify a workflow file, and open a pull request. If the runner is registered for that repo, the forked workflow may execute on the runner — giving an attacker arbitrary code execution on the Beelink.

This decision codifies when it's safe to register the runner for a repository and which trigger patterns are acceptable.

## Options Considered

### Option A: Runner for all repos, gate with workflow triggers

Register the runner for every repo. Rely on `on: push` (not `on: pull_request`) to prevent fork-based execution.

- **Pro:** Simple — one runner, all repos
- **Pro:** `push` events only fire for users with write access
- **Con:** A misconfigured workflow (e.g., `on: pull_request_target` with checkout of PR head) can still execute attacker code
- **Con:** Any new workflow added to a public repo must be audited for safe triggers
- **Verdict:** Too fragile — one mistake exposes the machine

### Option B: Runner only for private repos + gated public repos

Register the runner for private repos freely. For public repos, only register if workflows are tightly controlled (main-only triggers, no PR-based execution of untrusted code).

- **Pro:** Private repos are safe by definition — only collaborators can trigger workflows
- **Pro:** Public repos can still use the runner with careful trigger design
- **Con:** Requires discipline when adding workflows to public repos
- **Verdict:** Chosen — matches the trust boundary (write access = trusted)

### Option C: Never use self-hosted runners for public repos

Only register the runner for private repos. Public repos use GitHub-hosted runners or polling.

- **Pro:** Eliminates the risk entirely for public repos
- **Con:** Some public repo workflows genuinely need Beelink access (e.g., homelab deploy)
- **Verdict:** Too restrictive — homelab deploy needs the runner

## Decision

**Register the beelink runner for private repos freely. For public repos, only use the runner with main-only push triggers — never for PR-based workflows that could execute fork content.**

### Policy

| Repo visibility | Runner registration | Trigger rules |
|----------------|-------------------|---------------|
| Private | ✅ Always safe | Any trigger is fine |
| Public | ⚠️ Case-by-case | `on: push` to main only. Never `pull_request` with untrusted checkout. |

### Current repos

| Repo | Visibility | Runner registered | Trigger pattern | Why |
|------|-----------|-------------------|-----------------|-----|
| ColinCee/homelab | Public | ✅ | push to main, workflow_dispatch, issue_comment (trusted actors only) | Deploy, agent dispatch |
| ColinCee/notes | Private | ✅ (planned) | push to main | Auto-ingest notes into pgvector |
| ColinCee/flight-tracker-backend | Public | ❌ | N/A — uses GHCR polling | Public repo, can't trust fork PRs |

### Why flight-tracker uses polling

The flight-tracker backend is a public repo. Registering the beelink runner would allow anyone to fork it, add a malicious workflow, and execute arbitrary code on the Beelink. Instead, the homelab polls GHCR for new images every 30s using a systemd timer (`flight-tracker-poll.timer`). This is a pull model — the Beelink decides when to check, and only pulls pre-built Docker images (no code execution from the external repo).

## References

- [GitHub docs: Self-hosted runner security](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners#self-hosted-runner-security)
- `stacks/flight-tracker/flight-tracker-poll.timer` — polling pattern for public repos
- Issue #185 — notes auto-ingest using runner on private repo
