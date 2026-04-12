# ADR-004: Isolated Agent Service

**Date:** 2026-04-07
**Status:** Accepted

## Context

The homelab runs a self-hosted Copilot-powered agent on the Beelink. The
original direct-review approach lacked codebase context and mixed the model with
credentials and host-adjacent access that should stay outside the LLM boundary.

The accepted architecture has since grown beyond PR review: the same service now
handles trusted issue implementation, review/fix loops, and GitHub-side
orchestration. This ADR documents the current architecture and the tradeoffs
that make that safe enough to operate.

## Options Considered

### Option A: GitHub-hosted review only

Use GitHub's built-in Copilot review and keep implementation manual.

- ✅ Zero infrastructure to run
- ✅ Full codebase context via GitHub's platform
- ❌ No control over prompts, output format, or orchestration
- ❌ Doesn't support self-hosted implementation workflows
- **Verdict:** Rejected. Too limited for the automation goals.

### Option B: FastAPI orchestrator + isolated Copilot CLI worktrees

Run Copilot CLI headless inside an isolated container. The CLI gets local repo
files inside disposable worktrees; the orchestrator owns git operations,
workflow state, and all GitHub API calls.

- ✅ Full codebase context with local file access and tool use
- ✅ Source-controlled prompts and `.github` instructions
- ✅ Credential isolation: the CLI does not get GitHub API tokens
- ✅ Reusable architecture for both review and implementation
- ✅ Parallel worktree model without cloning the repo for every task
- ❌ More moving parts than a hosted-only solution
- **Verdict: Chosen.** Best balance of capability, control, and isolation.

### Option C: Direct API calls with enriched context

Keep the agent mostly stateless and push more prompt context into API calls.

- ✅ Simpler runtime model
- ❌ Still no local tool use or codebase-directed exploration
- ❌ Harder to keep prompts and output contracts reliable
- ❌ Doesn't solve the isolation boundary cleanly
- **Verdict:** Rejected. Too weak on capability and safety.

## Decision

Use a single isolated agent service with a narrow split of responsibilities:

- **Copilot CLI owns local reasoning and file edits**
- **The FastAPI orchestrator owns git, GitHub API calls, metrics, and cleanup**

Exact request/response models and payload shapes live in source (`main.py`,
`review.py`, `implement.py`); the ADR only owns the workflow contract and the
tradeoffs around it.

## Architecture

```text
GitHub Actions
  ├─ PR comment "/review"
  │    → .github/workflows/code-review.yaml
  │    → POST /review
  └─ Issue label "agent" or comment "/implement"
       → .github/workflows/implement.yaml
       → POST /implement

Agent service (FastAPI)
  → create/fetch worktree from bare clone
  → run Copilot CLI headless in that worktree
  → validate CLI output
  → commit/push bot-owned branches when implementing
  → create PRs and post reviews via GitHub App
  → mark worktrees for deferred cleanup
```

## Workflow Contract

| Flow | Current behavior |
|------|------------------|
| Review trigger | Manual only: `/review` on a PR comment. No auto-review on open/synchronize. |
| Implement trigger | Issue `agent` label or `/implement` comment from a trusted user. |
| Implement lifecycle | Create `agent/issue-<N>` branch → run Copilot → commit/push → open PR → review/fix loop up to 3 fix attempts. |
| Review/fix sessions | The implementor session is resumed for fixes; each review round is a fresh reviewer session. |
| Review authority | Bot reviews are advisory. CI and human judgment gate merges. |

### Review-specific gotchas

- GitHub rejects `APPROVE` and `REQUEST_CHANGES` on the bot's own PRs, so
  self-reviews are downgraded to `COMMENT`. That means unresolved-thread
  behavior differs from normal human PR reviews.
- Inline comments outside diff hunks are moved into the review body before
  posting. If GitHub still rejects inline comments with HTTP 422, the
  orchestrator retries without them and appends the rejected comments to the
  review body instead.

## Isolation Model

### Credentials

| Credential | Used by | Purpose |
|------------|---------|---------|
| `COPILOT_GITHUB_TOKEN` | Copilot CLI | Copilot Requests only |
| GitHub App token | Orchestrator | Read issues/PRs, push bot-owned branches, create PRs, post reviews/comments |

What the CLI does **not** get:

- Colin's personal `gh` auth
- GitHub App credentials
- Docker socket
- Host directory mounts

### Filesystem and worktrees

| Path | Role |
|------|------|
| `/repo.git` | Persistent bare clone used as the shared object store |
| `/reviews/` | Disposable worktrees for PR reviews and agent branches |

Worktrees are not deleted immediately on success/failure. The service writes a
cleanup marker when the worktree is created and refreshes it again at teardown.
That makes crash-orphaned worktrees reapable after the retention window instead
of leaving unrecoverable directories behind. The exact retention value and
marker schema remain source-owned in `stacks/agents/app/git.py`.

### Bot-owned branch policy

Agent-owned branches are disposable orchestration state, not collaboration
branches. Pushes use `--force` rather than `--force-with-lease` because reruns
that reused `agent/issue-*` branch names hit stale-info failures. That tradeoff
is acceptable because duplicate runs are blocked elsewhere and humans do not
share those branches.

## What Changed

| Aspect | Before | Current architecture |
|--------|--------|----------------------|
| LLM execution | Narrow direct-review flow | Copilot CLI in isolated worktrees |
| GitHub side effects | Mixed into model-adjacent flow | Orchestrator-only |
| Supported workflows | PR review | PR review + issue implementation + review/fix loop |
| Review trigger | Planned automation variants | Manual `/review` comment contract |
| Worktree cleanup | Immediate removal assumption | Deferred cleanup via retention markers |

## References

- `.github/workflows/code-review.yaml`
- `.github/workflows/implement.yaml`
- `stacks/agents/app/main.py`
- `stacks/agents/app/review.py`
- `stacks/agents/app/implement.py`
- `stacks/agents/app/git.py`
