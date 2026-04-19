---
name: docs-audit
description: Audit documentation for staleness and gaps. Use when checking if docs are up to date, finding missing documentation, or doing a periodic docs refresh pass.
allowed-tools: shell
user-invocable: true
---

# Documentation Audit Skill

You are performing a documentation freshness audit. Your goal is to find docs that have drifted from reality, identify high-value gaps, and fix what's stale.

## When to use

- After completing a feature epic or major refactor
- Periodic maintenance ("are the docs still accurate?")
- When a new stack, service, or integration was added without updating all surfaces
- Before onboarding documentation to a new contributor or tool

## Ownership model

Read `docs/decisions/008-documentation-ownership.md` for the full policy. The key principle: each fact has one authoritative home. When multiple surfaces mention the same thing, they summarize and link rather than duplicating detail.

## Audit checklist

Work through these checks in order. For each surface, compare what the doc claims against what the codebase actually contains.

### 1. Stack inventory

Every stack in `stacks/` must appear in ALL of these:

| Surface | Location |
|---------|----------|
| README.md | "Current Services" table |
| docs/architecture.md | "What runs on the Beelink" table |
| .github/copilot-instructions.md | "Repo Layout" bullet and "Architecture" diagram |
| .github/instructions/stacks.instructions.md | "Current Stacks" table |

**How to check:** List directories in `stacks/`, then grep each surface for every stack name. Flag any that are missing.

### 2. Roadmap accuracy

`docs/roadmap.md` tracks active work and known limitations.

- Every issue referenced must still be open. Check with `gh issue view <N> --json state`.
- If "Active" items are closed, remove or archive them.
- Open epics or major planned work should have a row.

### 3. Architecture and deploy docs

`docs/architecture.md` describes what runs and how deploys work.

- Compare deploy workflow description against `.github/workflows/deploy.yaml`
- Check that network topology matches reality (Tailscale IP, port bindings)
- Verify external service polling description matches `scripts/deploy.sh` cases

### 4. Runbook accuracy

For each runbook in `docs/runbooks/`:

- Commands must still work (correct paths, container names, compose syntax)
- Referenced workflows, scripts, and files must still exist
- Environment variables and secrets must match `.env.example` files

### 5. Agent lifecycle

`docs/agent-lifecycle.md` describes the implement/review flow.

- Verify endpoints, source file references, and trust model still match the code
- Check that worker naming, volume mounts, and monitor behavior match `main.py` and `services/docker.py`

### 6. Copilot instructions and skills

- `.github/copilot-instructions.md` — toolchain table must match `mise.toml` tasks
- `.github/instructions/*.md` — conventions must still reflect current code patterns
- `.github/skills/*/SKILL.md` — referenced commands and files must exist

### 7. ADR currency

ADRs are append-only and don't go stale in the traditional sense, but check:

- Is any ADR's decision contradicted by current implementation? If so, it should be marked "Superseded" with a pointer to the new reality.
- Are there architecture/security decisions that lack an ADR? (Check for patterns that were decided but never written up.)

## Identifying high-value gaps

After the staleness check, assess what's missing:

- **New stack without a runbook** — if a stack has operational procedures beyond `docker compose up`, it deserves a runbook
- **Architecture decisions without an ADR** — if you spot a non-obvious design choice that involved tradeoffs, suggest an ADR
- **Missing troubleshooting** — if a service has known failure modes not documented anywhere

## Output format

Present findings as a table:

```markdown
## Stale

| Doc | Issue | Fix |
|-----|-------|-----|
| README.md | Missing "knowledge" stack | Add row to services table |

## Gaps (high-value if added)

| Gap | Value | Suggested location |
|-----|-------|--------------------|
| No ADR for knowledge base | Preserves architecture tradeoffs | docs/decisions/016-knowledge-base.md |
```

## Fixing

After presenting findings, fix all stale docs in a single commit:

1. Make the edits
2. Run `mise run ci` to validate (yamllint catches markdown-adjacent YAML issues)
3. Commit with `docs:` conventional prefix
4. Create a PR for review

Do NOT write ADRs without human input — they require tradeoff reasoning that only the author knows. Flag them as gaps and ask.

## What NOT to do

- Don't rewrite docs for style — only fix factual inaccuracies
- Don't add detail that belongs in source code (exact schemas, constants, timeouts)
- Don't create new docs without confirming the gap is real and high-value
- Don't update ADRs — they're append-only. If one is wrong, suggest a superseding ADR.
