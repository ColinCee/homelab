---
name: bot-implement
description: Implementation skill for the homelab review bot. Activated when the bot orchestrator dispatches an issue implementation or review-feedback fix — not for local or interactive use.
allowed-tools: shell
user-invocable: false
---

# Implementation Skill

You are implementing a GitHub issue. The issue details are provided in the prompt.

## Process

1. Read and understand the issue requirements
2. Explore the codebase to understand the relevant code and conventions
3. Make the necessary changes — follow existing patterns
4. Run `mise run ci` to validate everything (lint, typecheck, test, compose). Fix any failures before finishing.
5. Self-review against the checklist below before finishing

## Rules

- **Do NOT commit, push, or create pull requests** — the orchestrator handles all git operations
- **Do NOT run `git add`, `git commit`, `git push`, or `gh pr create`**
- Focus on making correct, complete code changes in the working directory
- Follow existing code patterns and conventions
- Keep changes minimal — solve the issue, don't refactor unrelated code

## Quality

- Every change should be tested if test infrastructure exists
- Prefer modifying existing tests over creating new test files
- If you add a new module, add a corresponding test file

## The Review

After you finish, there are up to 2 review rounds that check for bugs, security issues, breaking changes, and operational risk. If the review requests changes, you get a fix attempt — then a second review may follow. After the final fix, the PR auto-merges.

Because the review cycle is capped at 2 rounds (review → fix → review → fix → merge), **aim for zero blockers on the first review.** Self-review thoroughly before finishing.

### Pre-completion checklist

Before finishing your work, self-review against these questions:

- **Error handling:** If a multi-step operation produces state that matters (metrics, audit logs, side effects), ensure that state is captured regardless of which step fails. Think about the whole operation, not individual calls — one handler around the entire post-critical section beats wrapping each call separately.
- **New types or patterns:** If you introduce a new exception class, status value, or convention, grep for all call sites using the old version and migrate them. Don't leave a mix of old and new.
- **Security:** Are credentials kept out of logs, error messages, and command args? Are untrusted inputs validated?
- **Consistency:** If you added a new status value, enum, or pattern, is it handled everywhere it's consumed (including workflows, polling loops, API responses)?
- **Cascading effects:** If you changed a function signature or return value, did you update every caller?

## Gotchas

- **`mise run ci` includes type-checking (`ty`).** `ty` does not support `# type: ignore` comments — they are silently ignored. If the type checker complains about dynamic attribute access, use typed dataclasses or exceptions instead of monkey-patching attributes onto objects.
- **Docker service names don't resolve across separate compose stacks.** Use Tailscale IPs (`100.x.x.x`) or `host.docker.internal`, not hostnames like `service-name:port`, when one stack needs to reach another.
- **GitHub API rejects APPROVE and REQUEST_CHANGES on your own PRs.** If the bot creates a PR and then reviews it, the review must be downgraded to COMMENT or the API returns 422.

## Responding to Review Feedback

When fixing issues raised by the review bot (or any reviewer):

1. **Read all comments first.** Don't start fixing the first one you see. Read every comment and identify the underlying patterns — 5 comments about missing error handling are one issue, not five.
2. **Grep for the pattern.** For each identified pattern, search the full codebase for every instance. If the reviewer found it in one file, assume it exists in others.
3. **Fix everything in one pass.** Address all instances of all patterns in a single commit. Multiple round-trips waste tokens and time.
