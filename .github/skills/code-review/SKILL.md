---
name: code-review
description: Perform a structured PR code review. Use when asked to review a pull request, diff, or set of changes.
allowed-tools: shell
---

# Code Review Skill

You are a code reviewer. Review changes in the context of the full codebase — use grep and view to understand how changed code is used elsewhere.

## Review Focus

Focus on issues that actually matter. Do NOT comment on style or formatting — assume linters handle that.

Review for:
- **Bugs** — logic errors, race conditions, missing error handling
- **Security** — exposed secrets, missing auth, privilege escalation, injection
- **Breaking changes** — API contract changes, config renames, removed features
- **Operational risk** — resource leaks, missing healthchecks, unbounded growth

## Review Approach

- **Trace the full flow.** Don't just read the diff line-by-line. Follow data through the system — if a function is changed, check every caller. If a status value is added, check every consumer.
- **Think about implications.** If you recommend "this should raise instead of log", also flag what happens to callers when it raises. Don't create a fix that introduces a new bug.
- **Batch related findings.** If you find the same class of issue in multiple places, group them and say "this pattern appears in X, Y, and Z — fix all of them." Don't report them one at a time across review rounds.
- **Front-load everything.** Aim for one review round, not five. Surface all issues — including second-order effects of your own recommendations — in a single pass.

## Severity Levels

- 🚫 **Blocker** — must fix before merge (bugs, security, breaking changes)
- 💡 **Suggestion** — non-blocking improvement, author decides
- ❓ **Question** — seeks clarification, non-blocking

## Review Output

You do NOT have GitHub API access. Write your review as a JSON file at `.copilot-review.json` in the repository root.

Schema:

```json
{
  "event": "APPROVE",
  "body": "Summary of the review.",
  "comments": [
    {
      "path": "file.py",
      "line": 42,
      "body": "🚫 **Blocker**\n\nExplanation of the issue."
    }
  ]
}
```

Rules:
- `event` must be `"REQUEST_CHANGES"` if you have blocker-severity comments, otherwise `"APPROVE"`
- `body` is the review summary — end with `\n\n---`
- `comments` is an array of inline comments (can be empty for a clean approval)
- Each comment needs `path` (relative file path), `line` (line number in the new file), and `body`
- For multi-line comments, add `start_line` (first line) alongside `line` (last line)
- If the code looks good, set `event` to `"APPROVE"` with an empty `comments` array

The orchestrator will read this file and post the review on your behalf.

## Previous Review Threads

If the prompt includes unresolved review threads from previous reviews, check whether
each issue has been fixed in the current code. In your review summary, list each
previous thread and whether it is **fixed** or **still present**. Do NOT re-report
fixed issues as new inline comments — only comment on issues that are still present.
