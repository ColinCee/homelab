---
name: refactor
description: Code cleanup and refactoring skill. Use when deliberately improving code quality — flattening nesting, eliminating duplication, extracting helpers, or simplifying tests.
allowed-tools: shell
user-invocable: true
---

# Refactoring Skill

You are performing a deliberate cleanup pass. Unlike feature work (bot-implement), your goal is structural improvement — not adding behavior.

## When to use

- Nesting depth > 3 levels that can be flattened with early returns or extraction
- Duplicated code blocks (3+ copies of the same pattern)
- Test files with repeated boilerplate that should be in conftest.py
- Functions doing too many things that should be split
- Dead code, unused imports, stale comments

## Principles

- **Behavior stays the same.** Every refactoring must pass the existing tests without modification to assertions. Test *structure* can change (e.g., moving mocks to conftest), but test *contracts* must not.
- **Flat control flow.** Prefer early returns and guard clauses over nested if/else. Extract deeply nested blocks into named functions.
- **DRY error handling.** If the same try/except/log pattern appears 3+ times, extract a helper.
- **One pass, not one file.** When you find a pattern to fix, grep for all instances across the codebase. Fix them all in one commit.
- **Apply code quality conventions** from `.github/instructions/python.instructions.md` — they define complexity signals and when to extract.
- **Measure the improvement.** Before and after: count lines, nesting depth, number of duplicate blocks. Include the delta in the PR description.

## Process

1. **Audit first.** Read the files in scope and identify specific patterns to improve. Don't start editing until you have a list.
2. **Validate baseline.** Run `mise run ci` before any changes. All tests must pass.
3. **Make changes.** Work through the list methodically.
4. **Validate again.** Run `mise run ci` after changes. Zero regressions.
5. **Commit and PR.** Use `refactor:` conventional commit prefix. Include before/after metrics.

## What NOT to do

- Don't rename things for style preference — only rename if the current name is misleading
- Don't restructure modules or move files between packages unless the issue specifically asks for it
- Don't add new abstractions that only have one caller — wait until there are 2+
- Don't change public API signatures unless the issue explicitly requires it
- Don't "improve" tests by weakening assertions
