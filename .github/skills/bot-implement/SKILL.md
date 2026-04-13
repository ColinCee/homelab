---
name: bot-implement
description: Implementation skill for the homelab review bot. Activated when the bot orchestrator dispatches an issue implementation or review-feedback fix — not for local or interactive use.
allowed-tools: shell
user-invocable: false
---

# Implementation Skill

You are implementing a GitHub issue. The issue details are provided in the prompt. You have full repo access via `GH_TOKEN` — you own the entire lifecycle from code changes through merge.

## Process

1. Read and understand the issue requirements
2. Explore the codebase to understand the relevant code and conventions
3. Make the necessary changes — follow existing patterns
4. Run `mise run ci` to validate everything (lint, typecheck, test, compose). Fix any failures before finishing.
5. Self-review against the checklist below
6. Commit, push, and create a draft PR
7. Self-review the PR, fix issues (up to 2 rounds), then mark ready and merge

## Git Workflow

You start in a worktree on the `agent/issue-{N}` branch, set up by the orchestrator. From here:

1. **Commit** — use conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`). Add the trailer: `Co-authored-by: colins-homelab-bot[bot] <colins-homelab-bot[bot]@users.noreply.github.com>`
2. **Push** — `git push origin agent/issue-{N}`. Force-push is fine on agent branches.
3. **Create draft PR** — `gh pr create --draft --title "..." --body "Closes #N\n\n..."`. Always link the issue with `Closes #N` in the body.
4. **Wait for CI** — `gh pr checks --watch` until all checks pass. Fix failures before proceeding.
5. **Self-review** — review your own diff critically. Look for the issues in the checklist below. Fix anything you find and push again.
6. **Mark ready** — `gh pr ready` when CI passes and you're satisfied with the code.
7. **Merge** — `gh pr merge --squash --auto` to squash-merge after all checks pass.

## Rules

- Follow existing code patterns and conventions
- Keep changes minimal — solve the issue, don't refactor unrelated code
- Every change should be tested if test infrastructure exists
- Prefer modifying existing tests over creating new test files
- If you add a new module, add a corresponding test file

## Safety

- **Never force-push to main** — only push to your `agent/issue-{N}` branch
- **Never log, print, or commit tokens or credentials**
- **Never modify `.github/workflows/`** — workflow changes require human review
- **Never commit secrets, API keys, or credentials** to the repository
- **Never access or modify `docs/private/`** — these are encrypted files you cannot read

## Self-Review Checklist

Before creating the PR, self-review against these questions:

- **Error handling:** If a multi-step operation produces state that matters (metrics, audit logs, side effects), ensure that state is captured regardless of which step fails.
- **New types or patterns:** If you introduce a new exception class, status value, or convention, grep for all call sites using the old version and migrate them.
- **Security:** Are credentials kept out of logs, error messages, and command args? Are untrusted inputs validated?
- **Consistency:** If you added a new status value, enum, or pattern, is it handled everywhere it's consumed?
- **Cascading effects:** If you changed a function signature or return value, did you update every caller?

## Gotchas

- **`mise run ci` includes type-checking (`ty`).** `ty` does not support `# type: ignore` comments. Use typed dataclasses or exceptions instead of monkey-patching.
- **Docker service names don't resolve across separate compose stacks.** Use Tailscale IPs (`100.x.x.x`) or `host.docker.internal`.
- **GitHub API rejects APPROVE and REQUEST_CHANGES on your own PRs.** Self-reviews must use `COMMENT` event or `gh pr review --comment`.

## Responding to Review Feedback

When fixing issues raised by reviewers:

1. **Read all comments first.** Identify the underlying patterns — 5 comments about missing error handling are one issue, not five.
2. **Grep for the pattern.** For each identified pattern, search the full codebase for every instance.
3. **Fix everything in one pass.** Address all instances of all patterns in a single commit.
