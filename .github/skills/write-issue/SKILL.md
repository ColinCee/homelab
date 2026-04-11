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

> A single `/implement` call owns the full lifecycle: implement → review → fix → re-review, looping until clean or capped at 3 iterations. The implementor session is resumed for fixes. The reviewer is always a fresh session.

Not: "Make the implement flow better."

### 3. What the agent can't discover

Research findings, external API behavior, confirmed feasibility — things that require experimentation or access the agent won't have. Don't include things discoverable by reading the codebase.

> CLI session resumption is confirmed working: `copilot --resume=<id> -p "prompt"` in headless mode preserves ~68k cached tokens. Session ID is captured via `--share`.

Not: "The codebase uses FastAPI and has these 5 files..."

### 4. What must not break

Blast radius boundaries. The agent is good at making changes but needs to know which existing behavior is sacred.

> `/review` must continue to work independently for human-triggered reviews. Accumulated premium_requests must still be accurate for metrics.

## What to omit

- **File lists and function signatures** — the agent explores the codebase itself
- **Implementation order** — the agent handles dependency ordering
- **How to test** — the agent has `mise run ci` and testing conventions
- **Codebase structure** — the agent can read it; instructions and skills cover conventions
