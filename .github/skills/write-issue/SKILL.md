---
name: write-issue
description: Write a GitHub issue for the agent to implement. Use when creating issues, tickets, or feature requests that will be picked up by the bot-implement workflow.
allowed-tools: shell
---

# Writing Issues for Agent Implementation

Issues are the contract between you and the implementing agent. Describe the problem and what success looks like — not how to change the code.

## Structure

### Problem
What's broken, missing, or wrong. Include the current behavior and why it's a problem. Be specific — "the fix loop is broken" is better than "improve the fix loop."

### Desired behavior
What the system should do after implementation. Describe the end state, not the journey. An implementor who reads only this section should know what "done" looks like.

### Research findings (if applicable)
Include technical findings the agent can't discover on its own — API behavior confirmed through testing, CLI flags verified to work, external constraints discovered through experimentation. Don't include things the agent can find by reading the codebase.

### Architecture (if applicable)
When the issue involves a design decision that's already been made, describe it here. Use a diagram or pseudocode showing the flow. This is a decision, not an instruction — the agent should understand the WHY, not just follow steps.

### Constraints
Non-negotiable requirements: what must not break, limits, security boundaries, compatibility requirements. These are guardrails, not implementation steps.

## What to include

- **Observable behavior** — "the endpoint should return X when given Y"
- **Decisions already made** — architecture choices, tool selections, confirmed feasibility
- **Things the agent can't discover** — external API quirks, confirmed CLI behavior, environment-specific facts
- **What success looks like** — how to verify the implementation is correct
- **What must not break** — existing behavior that must be preserved

## What to omit

- **File-by-file change lists** — the agent should explore the codebase and decide what to change
- **Function signatures** — that's implementation detail, not requirements
- **Implementation order** — the agent knows dependency ordering
- **How to test** — the agent has `mise run ci` and knows the testing conventions

## Why this matters

Prescribing the HOW means the agent follows instructions instead of thinking. If the prescribed approach has a flaw, the agent implements the flaw. Describing the WHAT lets the agent find a better path — or catch problems you didn't anticipate.

The bot-implement skill already tells the agent to explore the codebase, follow existing patterns, and run CI. Trust that process. Your job is to define the destination, not draw the map.
