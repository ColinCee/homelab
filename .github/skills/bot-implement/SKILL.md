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
3. **Plan before coding.** Write out what you intend to change and why — which files, what each change does, what error cases you'll handle. Grep for code that does similar things and decide whether to reuse, wrap, or diverge. This catches design mistakes before you write code.
4. Make the necessary changes
5. Run `mise run ci` to validate everything (lint, typecheck, test, compose). Fix any failures before finishing.
6. Sanity-check the implementation against the checklist below
7. Commit, push, and create a draft PR
8. Wait for CI, mark ready, and merge

## Git Workflow

You start in a worktree on the `agent/issue-{N}` branch, set up by the orchestrator. From here:

1. **Commit** — use conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`). Add the trailer: `Co-authored-by: colins-homelab-bot[bot] <colins-homelab-bot[bot]@users.noreply.github.com>`
2. **Push** — `git push origin agent/issue-{N}`. Force-push is fine on agent branches.
3. **Create draft PR** — `gh pr create --draft --title "..." --body "Closes #N\n\n..."`. Always link the issue with `Closes #N` in the body.
4. **Wait for CI** — `gh pr checks --watch` until all checks pass. Fix failures before proceeding.
5. **Stop** — do NOT mark the PR ready or merge it. The orchestrator handles review, merge, and marking the PR ready after automated review passes.

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
- **If an issue includes out-of-scope work** (e.g., workflow edits), implement everything you can and list the remaining items under a "Human follow-up" heading in the PR description. Do not attempt to modify restricted files.

## Implementation Checklist

Before creating the PR, sanity-check these questions:

- **Error handling:** If a multi-step operation produces state that matters (metrics, audit logs, side effects), ensure that state is captured regardless of which step fails.
- **New types or patterns:** If you introduce a new exception class, status value, or convention, grep for all call sites using the old version and migrate them.
- **Security:** Are credentials kept out of logs, error messages, and command args? Are untrusted inputs validated?
- **Consistency:** If you added a new status value, enum, or pattern, is it handled everywhere it's consumed?
- **Cascading effects:** If you changed a function signature or return value, did you update every caller?
- **Code quality:** If a function grows past ~80 lines or accumulates `try`/`except` blocks, stop and extract. See `.github/instructions/python.instructions.md` for conventions.

## Gotchas

- **`mise run ci` includes type-checking (`ty`).** `ty` does not support `# type: ignore` comments. Use typed dataclasses or exceptions instead of monkey-patching.
- **Docker service names don't resolve across separate compose stacks.** Use Tailscale IPs (`100.x.x.x`) or `host.docker.internal`.
- **Do not merge the PR.** The orchestrator runs an automated review-fix loop after your implementation, then handles merge itself. Merging from the CLI session bypasses the review loop.

## Responding to Review Feedback

When fixing issues raised by reviewers:

1. **Read all comments first.** Identify the underlying patterns — 5 comments about missing error handling are one issue, not five.
2. **Grep for the pattern.** For each identified pattern, search the full codebase for every instance.
3. **Fix everything in one pass.** Address all instances of all patterns in a single commit.
