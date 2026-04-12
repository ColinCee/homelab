# ADR-008: Documentation Ownership and Refresh Triggers

**Date:** 2026-04-12
**Status:** Accepted

## Context

The repo already follows a source-first philosophy, but contributors had no
stable contract for what belongs in code versus public docs, private docs, and
agent-facing `.github` docs. That ambiguity caused drift: README indexes lagged
behind the ADR set, agent lifecycle details diverged across docs, and important
tradeoffs ended up stranded in code comments, PRs, or review threads.

Contributors need one rule set that answers three questions without guessing:

1. Where should this fact live?
2. When does a code change require a docs change?
3. Which document is authoritative versus historical?

## Options Considered

### Option A: Keep documentation informal

Let each document decide its own scope case by case.

- ✅ Lowest process overhead
- ❌ Drift is inevitable because ownership stays ambiguous
- ❌ Important tradeoffs keep getting rediscovered in PRs and review loops
- ❌ Contributors cannot tell when a change requires docs work
- **Verdict:** Rejected. This is the current failure mode.

### Option B: Source-first ownership matrix with explicit refresh triggers

Define one home for each class of fact, keep narrow implementation detail in
source, and make docs updates mandatory when architecture, security, workflow
contracts, or repeating patterns change.

- ✅ Matches the repo's source-first philosophy
- ✅ Makes drift visible because each surface has an explicit job
- ✅ Preserves public/private/agent-facing separation without duplicating facts
- ✅ Gives contributors a usable checklist for when docs must change
- **Verdict: Chosen.** It creates a stable contract without turning prose into a second codebase.

### Option C: Push nearly everything into source

Use code comments and docstrings for most knowledge; keep prose minimal.

- ✅ Minimal prose to maintain
- ❌ Hides architecture, security, and operational tradeoffs inside implementation files
- ❌ Leaves operators and future contributors without human-facing workflow docs
- ❌ Doesn't solve public/private/agent-facing separation
- **Verdict:** Rejected. Source should own exact behavior, not all explanation.

## Decision

Use the ownership rules below. One fact should have one authoritative home. When
other surfaces need to mention it, they should summarize the contract and link
to the owner instead of repeating exact detail.

| Surface | Owns | Does not own |
|---------|------|--------------|
| Source code / tests / docstrings | Exact schemas, constants, payloads, timeout defaults, parser assumptions, narrow implementation invariants, examples tied to validators | Architecture essays, operational procedures, status tracking |
| `README.md` | Repo map, quickstart, major services, and index into deeper docs | Full procedures, long tradeoff analysis, sensitive details |
| `docs/roadmap.md` | Active work, planned work, and current limitations | Accepted decisions, historical rationale, exact implementation contracts |
| ADRs in `docs/decisions/` | Accepted architecture/security/pattern decisions, tradeoffs, and the durable contract around them | Task tracking, copy-paste procedures, exact source-owned values |
| Runbooks in `docs/runbooks/` | Human/operator procedures, verification steps, operational gotchas, links to owning source files | Architecture debates, duplicated constant tables, sensitive secrets |
| Private docs in `docs/private/` | Sensitive topology, security operations, credential-handling procedures, incident notes | Public workflow contracts, duplicate copies of public architecture docs |
| Agent-facing docs in `.github/` | Guidance the model needs to act correctly: repo conventions, automation workflow contracts, and gotchas it will not infer | A second copy of schemas/constants/payloads that already live in source |

### Source-owned examples

These are intentionally owned by code, not prose:

- review output schema and examples → `stacks/agents/app/review.py`
- CLI timeout and stderr-based stats parsing → `stacks/agents/app/copilot.py`
- bot identity and 422 review fallback behavior → `stacks/agents/app/github.py`
- worktree cleanup markers, retention semantics, and agent branch push strategy → `stacks/agents/app/git.py`

## When Docs Must Change

Update the owning docs in the same change when you modify:

| Change type | Required doc updates |
|-------------|----------------------|
| Architecture boundary, deployment model, trust boundary, or security posture | Update the owning ADR; update README index and any affected runbook/private doc summaries |
| Human/operator workflow or verification steps | Update the owning runbook; update README only if the entry point or index changed |
| Agent workflow contract or non-obvious gotcha the model needs | Update the relevant `.github` instruction/skill; update public docs too if humans depend on that contract |
| Active/planned work or known limitation status | Update `docs/roadmap.md` |
| Sensitive operational or security procedure | Update the relevant private doc and keep public docs limited to non-sensitive summaries |

### When docs do **not** need prose changes

If a change only affects an exact schema, constant, payload shape, env var name,
timeout value, or parser detail — and the higher-level contract is unchanged —
update source/tests/docstrings only. Do not mirror those details into prose just
to keep documents "complete".

### Refresh rules

- Current behavior beats historical intent. If the system evolved, update the
  accepted doc or add a superseding ADR instead of leaving stale prose behind.
- When a fact moves to a better home, remove or replace stale copies with a link.
- Brevity matters, but surprising operational behavior must be documented where
  the operator or agent will trip over it.

## References

- `README.md` — top-level documentation index
- `.github/AUTHORING.md` — agent-facing documentation authoring
- `.github/instructions/docs.instructions.md` — docs editing conventions
- `stacks/agents/app/` — authoritative source for narrow agent behavior
