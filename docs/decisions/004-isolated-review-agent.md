# ADR-004: Isolated Code Review Agent

**Date:** 2026-04-07
**Status:** Accepted
**Requirements:** R16 (Docker socket hardening), R14 (intrusion detection)

## Context

The homelab has a self-hosted AI code review agent running on the Beelink. The current implementation (PR #27) has fundamental limitations:

1. **No codebase context** — the agent only receives the PR diff text. It can't check callers, types, imports, or project conventions. It's essentially a glorified linter.
2. **No credential isolation** — the agent container mounts Colin's `~/.config/gh` directory, giving it full access to GitHub (push, merge, delete repos, bypass branch protection).
3. **No network isolation** — the container can reach any service on the local network (Dokploy, other containers, host SSH).
4. **No filesystem isolation** — host directories are mounted read-only but still accessible.

These issues mean any prompt injection or model misbehaviour could have real consequences — merging PRs, modifying repos, or accessing local services.

## Options Considered

### Option A: `gh pr edit --add-reviewer @copilot`

Use GitHub's built-in Copilot code review. One CLI command, zero infrastructure.

- ✅ Full codebase context via GitHub's infrastructure
- ✅ Zero maintenance
- ❌ No control over review prompt, severity model, or output format
- ❌ No self-hosted — depends entirely on GitHub's service
- **Verdict:** Too limited for customisation needs. Good fallback.

### Option B: Copilot CLI headless in isolated container

Run Copilot CLI in non-interactive mode (`-p` flag) inside a locked-down Docker container. The CLI has full tool use (grep, read files, search codebase) but the container has no access to the host or Colin's credentials.

- ✅ Full codebase context (grep, file reading, LSP)
- ✅ Reads `.github/copilot-instructions.md` automatically
- ✅ Standalone binary — no Node.js runtime needed
- ✅ JSON output (`--output-format json`) for structured parsing
- ✅ Credential isolation via fine-grained PAT + GitHub App
- ✅ Network isolation via egress proxy
- ✅ Parallel reviews via git worktrees
- ❌ More infrastructure to maintain
- **Verdict: Chosen.** Best balance of capability and security.

### Option C: Enrich current agent with file context

Keep the FastAPI + direct Copilot API approach but clone the repo and send full file contents alongside the diff.

- ✅ Simple enhancement to existing code
- ❌ Still no tool use — model can't decide what context it needs
- ❌ Doesn't solve credential/network isolation
- **Verdict:** Insufficient. Doesn't address the core problems.

## Decision

Implement Option B: Copilot CLI headless review in a fully isolated container.

## Architecture

```
PR event → GitHub Actions workflow
    → Tailscale connect to Beelink
    → POST http://beelink:8585/review (fire-and-forget, returns 202)

Review agent container (async):
    → git fetch origin pull/<N>/head
    → git worktree add /reviews/pr-<N>
    → copilot -p "Review this PR" \
        --model gpt-5.4 --output-format json \
        -s --yolo --no-ask-user --effort high
    → Parse output → Post review via GitHub App token
    → git worktree remove /reviews/pr-<N>
```

## Isolation Model

### Credentials (principle of least privilege)

| Credential | Purpose | Scope |
|---|---|---|
| `GH_TOKEN` (fine-grained PAT) | Copilot CLI LLM access | "Copilot Requests" permission only |
| GitHub App private key | Post reviews, read PR contents | Pull Requests (write), Contents (read) |

What the container does NOT get:
- ❌ Colin's `~/.config/gh` — full GitHub auth never enters the container
- ❌ Docker socket — can't manage other containers
- ❌ `--privileged` flag — standard container sandbox
- ❌ Host directory mounts — no access to host filesystem

### Network isolation (egress whitelist)

The review container sits on a Docker internal network with no direct internet access. A Squid proxy sidecar provides filtered egress:

| Domain | Purpose |
|---|---|
| `github.com` | Git clone/fetch |
| `api.github.com` | Post reviews, fetch PR data |
| `api.githubcopilot.com` | Copilot LLM API |

All other domains are blocked. The container cannot reach local network services (Dokploy, Grafana, SSH, etc.).

### Filesystem isolation

- Bare clone at `/repo.git` — persistent Docker volume (object cache), not a host mount
- Worktrees in `/reviews/` — tmpfs, ephemeral, wiped on restart
- `read_only: true` root filesystem
- No host directory mounts at all

### Runtime hardening

```yaml
read_only: true
security_opt: [no-new-privileges:true]
cap_drop: [ALL]
mem_limit: 2g
cpus: 2.0
```

## Parallel Reviews

Git worktrees from the bare clone, each in tmpfs:

```
/repo.git          (bare clone — object store only)
/reviews/
├── pr-42/         (worktree for PR 42)
├── pr-43/         (worktree for PR 43)
└── pr-44/         (worktree for PR 44)
```

Each review is independent. Worktrees are cleaned up after review completes.

## GitHub App

Create a GitHub App `homelab-review-bot` installed only on `ColinCee/homelab`:

- **Permissions:** Pull Requests (write), Contents (read)
- **Identity:** Reviews appear as `homelab-review-bot[bot]`
- **Auth:** JWT from private key → short-lived installation token (1hr expiry)
- The App's approval satisfies branch protection's review requirement
- Agents (Copilot coding agent, etc.) can create PRs but cannot approve them — only the review bot can

## What Changes

| Aspect | Before | After |
|---|---|---|
| LLM access | Direct Copilot API (diff only) | Copilot CLI headless (full codebase) |
| Auth | Colin's `gh` config mounted | Fine-grained PAT + GitHub App |
| Review identity | `github-actions[bot]` | `homelab-review-bot[bot]` |
| Container isolation | Shared host credentials | Locked down, egress filtered |
| Workflow | Synchronous (wait for response) | Fire-and-forget (async) |
| Concurrency | Single-threaded | Parallel via git worktrees |

## What We Keep

- FastAPI as the trigger endpoint (POST /review → 202 Accepted)
- Tailscale-based connectivity from Actions to Beelink
- Review philosophy from `.github/copilot-instructions.md` (loaded by Copilot CLI automatically)

## Implementation Steps

1. Create GitHub App + install on repo
2. Create fine-grained PAT with Copilot Requests only
3. Build new Dockerfile (Python + standalone copilot binary + git)
4. Write compose config with isolation (read_only, cap_drop, internal network)
5. Add Squid egress proxy sidecar with domain whitelist
6. Implement async fire-and-forget endpoint with worktree management
7. Implement Copilot CLI review logic + JSON output parsing
8. Implement GitHub App JWT auth for posting reviews
9. Simplify Actions workflow (fire-and-forget trigger)
10. Update branch protection to recognise the App
11. Run evaluation matrix (clean, bug, security, borderline PRs)
12. Clean up old direct Copilot API code

## References

- [Copilot CLI non-interactive mode](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/use-copilot-cli)
- [Copilot CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)
- [GitHub Apps documentation](https://docs.github.com/en/apps/creating-github-apps)
- [Docker egress filtering](https://docs.docker.com/engine/network/firewall-iptables/)
