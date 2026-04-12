# Copilot Authoring Guide

How to write and organize instructions and skills for this repo. Covers what goes where, how each mechanism works, and how to write effective content.

Repo-wide documentation ownership lives in
`docs/decisions/008-documentation-ownership.md`. Use that ADR as the single
policy for code, README, public docs, private docs, and `.github/` docs. This
guide only explains how the `.github/` layer fits into that system.

## Mechanisms

| Mechanism | Location | When loaded | Use for |
|-----------|----------|-------------|---------|
| **Global instructions** | `.github/copilot-instructions.md` | Every conversation | Repo layout, design philosophy, toolchain, architecture |
| **Scoped instructions** | `.github/instructions/*.md` | When `applyTo` glob matches open files | Language conventions, stack patterns, testing rules |
| **Skills** | `.github/skills/<name>/SKILL.md` | On-demand when the model decides they're relevant | Specialized workflows for automation (review, implement) |

### Global instructions (`copilot-instructions.md`)

Always injected into every Copilot conversation — local, CLI, cloud agent. Keep this small and universal. Every token here competes with the user's actual task.

**Put here:** repo structure, design philosophy, key commands, top-level workflow contracts.
**Don't put here:** language-specific rules, exact schemas/constants/payloads, or procedures that belong in runbooks/skills.

### Scoped instructions (`instructions/*.md`)

Injected when the user has files open matching the `applyTo` glob in the frontmatter. Invisible otherwise. Use these for conventions that only matter in specific parts of the codebase.

```yaml
---
applyTo: "stacks/agents/app/**/*.py"
---
```

**Put here:** language conventions, module design rules, testing patterns, framework-specific guidance.
**Don't put here:** universal repo info (that's global instructions), automated workflows (that's skills), or source-owned exact values that should stay in code.

### Skills (`skills/<name>/SKILL.md`)

Loaded on-demand when the model decides the skill is relevant based on the `name` and `description` fields. Skills can include scripts, references, and templates alongside the SKILL.md.

**Put here:** step-by-step workflows, automation procedures, output format templates, gotchas agents will not infer from source alone.
**Don't put here:** coding conventions (use instructions), repo overview (use global instructions), or duplicate copies of source-owned contracts.

## Writing scoped instructions

### Frontmatter

```yaml
---
applyTo: "stacks/**"          # glob pattern — when to inject
---
```

The `applyTo` glob determines when the instructions are loaded. Use the narrowest glob that covers the relevant files.

### Content guidelines

- **Source code is the single source of truth.** Don't duplicate schemas, constants, or examples that live in code — they will go stale. Reference the source file and tell the model to read it. If it can drift from the code, it shouldn't be written in prose.
- **Add what the model lacks, skip what it knows.** Don't explain what Docker is. Do explain that this repo binds ports to `100.100.146.119` (Tailscale interface).
- **Be prescriptive where it matters.** "Use `asyncio.to_thread()` for blocking I/O" beats "consider async patterns."
- **Tables over prose** for comparisons, tool lists, and conventions.
- **Keep each file focused** on one domain — don't combine Python conventions with Docker patterns.

## Writing skills

Skills follow the [Agent Skills spec](https://agentskills.io/specification). They work across VS Code, Copilot CLI, and the cloud coding agent.

### Frontmatter

```yaml
---
name: my-skill                # must match directory name, lowercase + hyphens
description: >-               # what it does AND when to use it — this is how the model decides to load it
  Short, keyword-rich description. Be specific about the trigger conditions.
allowed-tools: shell           # pre-approves shell commands (additive, not restrictive)
user-invocable: false          # hide from /slash menu (use for orchestrator-only skills)
disable-model-invocation: true # prevent auto-loading (use for manual-only skills)
---
```

| Field | Required | Notes |
|-------|----------|-------|
| `name` | Yes | Lowercase, hyphens only. Max 64 chars. Must match directory name. |
| `description` | Yes | Max 1024 chars. Include keywords naturally — don't append "Keywords: ..." |
| `allowed-tools` | No | Space-separated pre-approved tools. `shell` is common. |
| `user-invocable` | No | Default `true`. Set `false` for orchestrator-only skills. |
| `disable-model-invocation` | No | Default `false`. Set `true` for manual `/skill-name` only. |

#### Invocation matrix

| user-invocable | disable-model-invocation | Slash cmd | Auto-loaded | Use case |
|:-:|:-:|:-:|:-:|---|
| true | false | ✅ | ✅ | General-purpose |
| false | false | ❌ | ✅ | Orchestrator / background |
| true | true | ✅ | ❌ | Manual on-demand |
| false | true | ❌ | ❌ | Effectively disabled |

### Content guidelines

From [agentskills.io best practices](https://agentskills.io/skill-creation/best-practices):

- **Procedures over declarations.** Teach how to approach a class of problems, not what to produce for a specific instance.
- **Gotchas are high-value.** Environment-specific facts that defy reasonable assumptions — the model will get these wrong without being told.
- **Match specificity to fragility.** Be prescriptive for fragile operations (exact commands, exact sequence). Give freedom where multiple approaches are valid.
- **Progressive disclosure.** Keep SKILL.md under 500 lines / 5000 tokens. Move detailed references to `references/` and tell the model when to load them.

### Effective patterns

**Gotchas section** — concrete corrections to mistakes the model will make:
```markdown
## Gotchas
- `ty` does not support `# type: ignore` — use typed alternatives instead.
- Docker service names don't resolve across separate compose stacks.
```

**Validation loops** — make the model check its own work:
```markdown
1. Make changes
2. Run `mise run ci`
3. If failures, fix and re-run
4. Only finish when CI passes
```

**Checklists** — prevent skipped steps:
```markdown
## Pre-completion checklist
- Error handling: are all failure paths covered?
- New patterns: are all call sites migrated?
- Security: are credentials out of logs and args?
```

**Source references** — point at code instead of duplicating it:
```markdown
Read `stacks/agents/app/review.py` for the `ReviewOutput` and
`ReviewComment` models — they define the schema and include examples.
```

## Decision tree: where does this go?

```
Is it universal to every conversation?
  → Yes → copilot-instructions.md

Is it specific to certain file types or directories?
  → Yes → instructions/<scope>.instructions.md with applyTo glob

Is it a workflow with steps, output formats, or scripts?
  → Yes → skills/<name>/SKILL.md

Is it only for the automated pipeline, not local use?
  → Yes → skill with user-invocable: false
```

## When `.github/` docs must change

- Update `.github/copilot-instructions.md` when repo-wide workflow contracts or
  high-level architecture guidance changes.
- Update scoped instructions when the conventions for a language, directory, or
  framework change.
- Update skills when the automation workflow, trigger semantics, or gotchas the
  model will not infer have changed.
- If a change only updates an exact schema, constant, timeout, env var name, or
  payload shape, keep the authoritative change in source and link to that source
  instead of copying it into `.github/`.

## Validation

Install the [skills-ref](https://github.com/agentskills/agentskills) library to validate skills:

```bash
pip install skills-ref
python -c "from skills_ref import validate; validate('.github/skills/my-skill/SKILL.md')"
```

## References

- [Agent Skills specification](https://agentskills.io/specification)
- [Best practices](https://agentskills.io/skill-creation/best-practices)
- [VS Code agent skills docs](https://code.visualstudio.com/docs/copilot/customization/agent-skills)
- [VS Code custom instructions docs](https://code.visualstudio.com/docs/copilot/customization/custom-instructions)
