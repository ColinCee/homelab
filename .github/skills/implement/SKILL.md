---
name: implement
description: Implement a GitHub issue. Make code changes, run tests, and ensure quality.
allowed-tools: shell
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

## The Review Cycle

After you finish, an automated review bot will review your changes. It checks for bugs, security issues, breaking changes, and operational risk. Each review round costs time and tokens — **aim for zero blockers on the first review.**

### Pre-completion checklist

Before finishing your work, self-review against these questions:

- **Error handling:** If a multi-step operation produces state that matters (metrics, audit logs, side effects), ensure that state is captured regardless of which step fails. Think about the whole operation, not individual calls — one handler around the entire post-critical section beats wrapping each call separately.
- **New types or patterns:** If you introduce a new exception class, status value, or convention, grep for all call sites using the old version and migrate them. Don't leave a mix of old and new.
- **Security:** Are credentials kept out of logs, error messages, and command args? Are untrusted inputs validated?
- **Consistency:** If you added a new status value, enum, or pattern, is it handled everywhere it's consumed (including workflows, polling loops, API responses)?
- **Cascading effects:** If you changed a function signature or return value, did you update every caller?

## Responding to Review Feedback

When fixing issues raised by the review bot (or any reviewer):

- **Fix the category, not the instance.** Each comment is a symptom — look for the underlying pattern and audit the full codebase for the same class of issue. If a reviewer catches a silent API failure in one function, check every API call for the same problem.
- **Generalise before you start coding.** Before touching any file, read all the review comments and identify themes. Group related findings and fix them as a batch.
- **Carry context forward.** Understand *why* the original code was written that way. Read the issue, PR description, and any previous review threads before making changes.
- **Don't be literal.** A comment pointing at line 42 might reveal a design problem that affects lines 10–100. Step back and consider the broader implications.
- **One pass, not three.** Aim to address all findings — and their generalisations — in a single fix cycle. Multiple round-trips waste tokens and time.
