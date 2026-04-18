# ADR-010: Agent Security Model — CLI Autonomy

**Date:** 2026-04-13
**Status:** Accepted
**Supersedes:** ADR-004 (credential isolation model), ADR-007 (network isolation rationale)

## Context

ADR-004 established a split-responsibility model: the Copilot CLI owns local
reasoning and file edits, the FastAPI orchestrator owns git, GitHub API calls,
and credentials. The CLI was sandboxed — `GH_TOKEN` explicitly stripped from its
environment (`copilot.py` line 154), no ability to commit, push, or call the
GitHub API.

This was the right call when the agent was unproven. After 10+ successful PRs,
the orchestrator's complexity (~2000 LOC across implement.py, git.py, github.py,
review.py) is now the primary source of bugs:

- Stats parsing breaks every time the CLI changes output format
- Container restarts kill in-flight work because the orchestrator holds all
  state in-process
- Review file parsing, progress comments, merge polling — all custom code that
  the CLI handles natively when given access
- Every new capability (rebase, conflict resolution) requires more orchestrator
  code

The CLI is capable of managing its own git operations, PR lifecycle, and review
cycle. The security model needs to shift from credential isolation to scope and
containment.

## Decision

Give the CLI full repo access via `GH_TOKEN` (GitHub App installation token).
The orchestrator becomes a thin dispatcher: receive webhook, validate trust, set
up worktree, spawn CLI, collect stats, report metrics.

The security boundary shifts from **"CLI cannot touch GitHub"** to **"CLI
operates within a scoped, contained environment with external enforcement."**

## Attack Vectors and Mitigations

### 1. Prompt injection

**Vector:** Malicious content in issue bodies, PR descriptions, or code comments
could instruct the CLI to exfiltrate secrets, push to main, or perform
destructive actions.

**Mitigations:**

| Control | Enforcement |
|---------|-------------|
| Actor allowlist | Hardcoded `ALLOWED_ACTORS` in `trust.py` — single source of truth. Both `/review` and `/implement` endpoints reject unknown triggering actors with 403. Workflows also gate on `author_association` (free, prevents the run). |
| Content trust | Issue/PR bodies injected into CLI prompts are checked against `ALLOWED_ACTORS` via `user.login`. Linked issues from untrusted authors are silently skipped; untrusted issue authors for `/implement` raise `ValueError`. Prevents prompt injection via attacker-controlled content. |
| Fork PR rejection | Review endpoint rejects fork PRs (where the head repo differs from the base repo, including deleted forks). Prevents untrusted code from triggering agent reviews with GH_TOKEN. |
| Skill file separation | The `.github/skills/` files are trusted instructions committed by repo owners. Untrusted content (issue bodies) is clearly delineated as user input in the prompt, not system instructions. |
| Branch protection | Even if the CLI is tricked into pushing to main, GitHub branch protection rules reject it. Requires CI to pass and blocks force-push. |
| Token scope | The App token is scoped to a single repository. Even a fully compromised CLI cannot access other repositories, org settings, or admin operations. |
| Token redaction | CLI output is scrubbed of known secret values before logging or error reporting. Prevents accidental token leakage in logs or GitHub comments. |

**Residual risk:** A sophisticated injection could instruct the CLI to create a
PR with malicious code that passes CI. Mitigated by human review — all PRs
require a human to merge or at minimum a human to close if auto-merged with bad
content. This is the same risk as any developer with repo write access.

### 2. Token exfiltration

**Vector:** The CLI could leak `GH_TOKEN` to stdout, log files, committed code,
or external services.

**Mitigations:**

| Control | Enforcement |
|---------|-------------|
| Token lifetime | GitHub App installation tokens expire after 1 hour. A leaked token has a limited window of usefulness. |
| Token scope | Single-repo scope with contents, issues, and PRs write. No admin, no org access, no delete-repo permission. |
| Skill instructions | Skill file explicitly instructs: "Never log tokens, never commit secrets, never include credentials in PR bodies or comments." |
| Log filtering | The orchestrator's stats parser reads CLI stdout — token values can be filtered before logging. |
| No outbound restriction | Per ADR-007, egress filtering is brittle against SaaS endpoints. The real control is token scope — even if exfiltrated, the token can only do what the App permissions allow. |

**Residual risk:** A 1-hour repo-scoped token could be used to push malicious
code or read private repo contents. Mitigated by: the repo is already
effectively public to anyone on the tailnet, and branch protection limits what
can be pushed to main.

### 3. Branch protection bypass

**Vector:** The CLI could attempt to force-push to main, delete branches, or
bypass required status checks.

**Mitigations:**

| Control | Enforcement |
|---------|-------------|
| GitHub branch protection | Main branch requires CI to pass, blocks force-push, blocks deletion. These are server-side rules — the CLI cannot bypass them regardless of token scope. |
| Skill instructions | CLI is instructed to only work on `agent/issue-{N}` branches. |
| App permissions | The App token does not have admin scope — cannot modify branch protection rules. |

**Residual risk:** None for main branch. Agent branches (`agent/*`) are
disposable and force-pushable by design — this is intentional and safe.

### 4. Container escape

**Vector:** A compromised CLI process could attempt to escape the container and
access the host.

**Mitigations:**

| Control | Enforcement |
|---------|-------------|
| `no-new-privileges: true` | Prevents privilege escalation via setuid/setgid binaries |
| `cap_drop: ALL` | Drops all Linux capabilities |
| `cap_add` (minimal) | Only CHOWN, FOWNER, SETUID, SETGID — needed for entrypoint user switching |
| Memory/CPU limits | `mem_limit: 2g`, `cpus: 2.0` — prevents resource exhaustion |
| Docker socket (API only) | API container has socket mount for spawning workers ([ADR-011](011-docker-socket-for-workers.md)). Worker containers do not get socket access. |
| No host filesystem | Only named volumes (`repo-cache`, `reviews`) — no secrets mounted |
| Non-root execution | `entrypoint.sh` drops to `agent:agent` user after volume setup |

**Residual risk:** Kernel exploits could bypass container isolation. Mitigated
by: standard Docker security posture, kept up to date via unattended-upgrades
on the host.

### 5. Secrets exposure

**Vector:** The CLI could access or commit sensitive data.

**Mitigations:**

| Control | Enforcement |
|---------|-------------|
| No secrets in repo | All credentials live in GitHub Actions secrets, not in the git repo |
| git-crypt | `docs/private/` is encrypted at rest. The CLI has no decryption key — these files appear as binary blobs in the worktree. |
| `access.md` gitignored | Local credentials file is never committed and doesn't exist in the container |
| Volume isolation | Named volumes (`repo-cache`, `reviews`) contain only git data — no host secrets mounted |
| No PEM in container | The GitHub App private key never enters the container. Tokens are minted by `actions/create-github-app-token` in the workflow and passed per-request. The PEM stays in GitHub Actions secrets. |
| Env allowlist | CLI subprocess gets only allowlisted env vars — `COPILOT_GITHUB_TOKEN`, `PATH`, `HOME`, etc. Server secrets (if any) are excluded. |

**Residual risk:** The CLI receives a 1-hour `GH_TOKEN` that could be
exfiltrated. Mitigated by: token scope (single-repo), short lifetime, and
output redaction.

### 6. Destructive actions

**Vector:** The CLI could delete the repository, remove branches, or corrupt
data.

**Mitigations:**

| Control | Enforcement |
|---------|-------------|
| No admin scope | The App token cannot delete the repository, transfer ownership, or modify settings |
| Branch protection | Main branch cannot be deleted or force-pushed |
| Agent branches are disposable | `agent/*` branches are expected to be created and deleted as part of the lifecycle |
| PR-based workflow | All changes go through PRs. Worst case: CLI creates a bad PR → human closes it |

**Residual risk:** The CLI could delete non-protected branches. These are
disposable by convention and easily recreated.

## What Changed from ADR-004 and ADR-007

| Aspect | ADR-004/007 (before) | ADR-010 (now) |
|--------|---------------------|---------------|
| CLI credentials | `COPILOT_GITHUB_TOKEN` only (inference) | + `GH_TOKEN` (repo access) |
| Git operations | Orchestrator exclusively | CLI (commit, push, branch, PR, merge) |
| GitHub API calls | Orchestrator exclusively | CLI via `gh` CLI or git credential |
| Security boundary | Credential isolation | Scope + containment + external enforcement |
| Orchestrator role | Full lifecycle management | Webhook receiver, trust validator, worker spawner |
| Blast radius of compromise | CLI can edit files in worktree only | CLI can push to agent branches, create/merge PRs, post comments |

The credential isolation boundary from ADR-004 is **intentionally removed**.
The network isolation rationale from ADR-007 remains valid — unrestricted
outbound is still the right call, but the justification now includes "CLI has
repo-scoped token" rather than "CLI has no GitHub token."

## Accepted Residual Risks

1. **CLI posts inappropriate content** to PRs/issues — mitigated by human
   oversight and the ability to close/revert
2. **CLI pushes large or malicious code** to agent branches — mitigated by
   branch protection (can't reach main without CI passing) and PR review
3. **Compromised CLI has repo write access** — mitigated by single-repo scope,
   1-hour token expiry, no admin permissions

These risks are equivalent to giving a junior developer scoped repo access with
branch protection enforced — which is exactly what the agent is.

## References

- [ADR-004: Isolated Agent Service](004-isolated-review-agent.md) — superseded
  credential isolation model
- [ADR-007: Agent Network Isolation](007-agent-network-isolation.md) —
  superseded credential scoping rationale (network model unchanged)
- [ADR-009: Capped Review Cycle](009-capped-review-cycle.md) — superseded
  lifecycle control now enforced by skill files
- `stacks/agents/app/services/copilot.py` — CLI subprocess wrapper, injects `GH_TOKEN`
- `stacks/agents/compose.yaml` — container security settings
- `.github/skills/bot-implement/SKILL.md` — skill-based control plane
