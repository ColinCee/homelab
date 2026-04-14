---
name: write-issue
description: Write a GitHub issue for the agent to implement. Use when creating issues, tickets, or feature requests that will be picked up by the bot-implement workflow.
allowed-tools: shell
---

# Writing Issues for Agent Implementation

An issue is a contract: define the destination, not the route. The agent can read the codebase, follow conventions, and run CI — your job is to tell it what problem to solve and how to know it's solved.

## The 4 essential sections

### 1. What's wrong

Current behavior and why it's a problem. Be specific about the gap.

> The fix loop uses a separate CLI session that starts cold — it re-reads the entire codebase without knowing WHY the original code was written that way. This is like handing code to a different developer for fixes.

Not: "The fix loop needs improvement."

### 2. What done looks like

Observable, testable success criteria. If the agent can't verify it with `mise run ci` or by checking behavior, it's not concrete enough.

> A single `/implement` call owns the full lifecycle: implement → self-review → fix (up to 2 rounds) → mark ready → merge. The CLI session handles all git operations, PR creation, and review.

Not: "Make the implement flow better."

### 3. What the agent can't discover

Research findings, external API behavior, confirmed feasibility — things that require experimentation or access the agent won't have. Don't include things discoverable by reading the codebase.

> CLI session resumption is confirmed working: `copilot --resume=<id> -p "prompt"` in headless mode preserves ~68k cached tokens. Session ID is captured via `--share`.

Not: "The codebase uses FastAPI and has these 5 files..."

### 4. What must not break

Blast radius boundaries. The agent is good at making changes but needs to know which existing behavior is sacred.

> `/review` must continue to work independently for human-triggered reviews. Accumulated premium_requests must still be accurate for metrics.

## Labels

Apply labels when creating or updating issues:

| Label | When to use |
|-------|-------------|
| `agent` | Triggers autonomous implementation. Add when the issue is ready for the bot. |
| `agent-failed` | Agent tried but couldn't complete. Needs human attention or a rewritten issue. |
| `priority/high` | Actively blocking or degrading agent reliability. Work on these first. |
| `priority/low` | Backlog — nice to have, no urgency. |
| `bug` | Something is broken. |
| `enhancement` | New feature or improvement. |
| `documentation` | Docs-only change. |

## Triggering implementation

After creating the issue, trigger the bot by commenting `/implement` on the issue or adding the `agent` label. The `implement.yaml` workflow dispatches to the agent service.

GitHub only auto-closes the issue on merge when the PR body uses a closing
keyword such as `Closes #N`, `Fixes #N`, or `Resolves #N`. `Implements #N` is
descriptive only.

## Critical: everything goes in the body

The implement bot only receives the issue **title and body**. It never sees issue comments, reactions, or linked PRs. All context, requirements, and constraints must be in the body itself. If you add information in a comment, the agent will never see it — rewrite the body instead.

## The issue body is also the review contract

The review bot checks PR changes against the linked issue's "what done looks
like" criteria. If the design evolves during implementation (e.g., a requirement
turns out to be unnecessary), update the issue body or explain the deviation in
the PR body. Otherwise the review bot will correctly flag the mismatch as an
unmet requirement.

## What to omit

- **File lists and function signatures** — the agent explores the codebase itself
- **Implementation order** — the agent handles dependency ordering
- **How to test** — the agent has `mise run ci` and testing conventions
- **Codebase structure** — the agent can read it; instructions and skills cover conventions
- **Workflow file changes** — the agent cannot modify `.github/workflows/`. If an issue requires workflow edits, note them in a separate "Out of scope (human follow-up)" section so the agent knows to skip them
