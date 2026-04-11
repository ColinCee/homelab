# ADR-008: Persistent Worktree Retention for Agent Tasks

**Date:** 2026-04-11
**Status:** Accepted

## Context

The agent service now drives both code review and issue implementation flows. The
original worktree lifecycle optimised for cleanup only:

1. `/reviews` was not a named Docker volume, so container restarts deleted every
   worktree, Copilot transcript, raw review JSON file, and `.copilot/` session
   directory.
2. `cleanup_worktree()` and `cleanup_branch_worktree()` removed worktrees
   immediately in `finally` blocks, leaving no debugging window after a task
   finished.
3. The unified implement lifecycle needs Copilot CLI `--resume`, which depends on
   the `.copilot/` directory surviving across multiple CLI invocations in the same
   retained worktree.

We need to preserve the latest task artifacts long enough to debug failures and
to let fix loops reuse the same Copilot session state, without letting `/reviews`
grow forever.

## Options Considered

### Option A: Keep eager deletion

Delete worktrees immediately after each task, as the original review flow did.

- ✅ Minimal disk usage
- ✅ No background cleanup logic
- ❌ Destroys `.copilot/` state, so `--resume` cannot work across retained tasks
- ❌ Removes raw artifacts needed for debugging failed or surprising runs
- **Verdict:** Rejected. It blocks the implement fix loop and leaves no debug window.

### Option B: Persist `/reviews` and defer cleanup with retention

Store worktrees on a named volume, write an expiry marker on cleanup, and reap old
worktrees later.

- ✅ Preserves transcripts, raw review JSON, `.copilot/`, and checked-out code
- ✅ Enables `--resume` for the unified implement lifecycle
- ✅ Keeps cleanup bounded with a configurable retention period
- ❌ Requires marker parsing and a startup/background reaper
- ❌ Uses more disk than eager deletion
- **Verdict: Chosen.** Best balance of debuggability, resume support, and bounded storage.

### Option C: Version every rerun (`pr-42-v1`, `pr-42-v2`, ...)

Keep every historical worktree instead of replacing the latest one.

- ✅ Full history for debugging across reruns
- ❌ More naming complexity and more disk growth
- ❌ Most debugging value is in the latest failed run, not every intermediate copy
- **Verdict:** Rejected. The extra history is not worth the operational cost right now.

## Decision

Persist `/reviews` as a named volume and switch worktree cleanup from eager delete
to deferred cleanup:

1. Worktree cleanup writes a `.cleanup-after` marker with an expiry timestamp and
   branch name instead of deleting immediately.
2. `reap_old_worktrees()` scans `/reviews/` and removes worktrees whose marker has
   expired.
3. The reaper runs on FastAPI startup and opportunistically before new worktree
   creation / cleanup operations.
4. Retention defaults to 14 days via `WORKTREE_RETENTION_SECONDS`, overridable by
   environment variable.
5. Re-reviews and reruns still use **replace semantics**: if `pr-42` or
   `agent/issue-59` already exists, remove that worktree first and recreate it from
   the latest remote state.

## References

- `stacks/agents/compose.yaml` — named volumes for `/repo.git` and `/reviews`
- `stacks/agents/app/git.py` — deferred cleanup marker and reaper implementation
- `stacks/agents/app/main.py` — startup reaper hook
- [ADR-004: Isolated Code Review Agent](004-isolated-review-agent.md) — original
  agent architecture
