# Copilot Customization Guide

How to write and organize instructions and skills for this repo. Covers what goes where, how each mechanism works, and how to write effective content.

## Mechanisms

| Mechanism | Location | When loaded | Use for |
|-----------|----------|-------------|---------|
| **Global instructions** | `.github/copilot-instructions.md` | Every conversation | Repo layout, design philosophy, toolchain, architecture |
| **Scoped instructions** | `.github/instructions/*.md` | When `applyTo` glob matches open files | Language conventions, stack patterns, testing rules |
| **Skills** | `.github/skills/<name>/SKILL.md` | On-demand when the model decides they're relevant | Specialized workflows for automation (review, implement) |

### Global instructions (`copilot-instructions.md`)

Always injected into every Copilot conversation — local, CLI, cloud agent. Keep this small and universal. Every token here competes with the user's actual task.

**Put here:** repo structure, design philosophy, key commands, architecture overview.
**Don't put here:** language-specific rules, workflow procedures, anything only relevant to certain files.

### Scoped instructions (`instructions/*.md`)

Injected when the user has files open matching the `applyTo` glob in the frontmatter. Invisible otherwise. Use these for conventions that only matter in specific parts of the codebase.

```yaml
---
applyTo: "stacks/agents/app/**/*.py"
---
```

**Put here:** language conventions, module design rules, testing patterns, framework-specific guidance.
**Don't put here:** universal repo info (that's global instructions), automated workflows (that's skills).

### Skills (`skills/<name>/SKILL.md`)

Loaded on-demand when the model decides the skill is relevant based on the `name` and `description` fields. Skills can include scripts, references, and templates alongside the SKILL.md.

**Put here:** step-by-step workflows, automation procedures, output format templates, gotchas.
**Don't put here:** coding conventions (use instructions), repo overview (use global instructions).

## Writing scoped instructions

### Frontmatter

```yaml
---
applyTo: "stacks/**"          # glob pattern — when to inject
---
```

The `applyTo` glob determines when the instructions are loaded. Use the narrowest glob that covers the relevant files.

### Content guidelines

- **Add what the model lacks, skip what it knows.** Don't explain what Docker is. Do explain that this repo binds ports to `100.100.146.119` (Tailscale interface).
- **Be prescriptive where it matters.** "Use `asyncio.to_thread()` for blocking I/O" beats "consider async patterns."
- **Tables over prose** for comparisons, tool lists, and conventions.
- **Keep each file focused** on one domain — don't combine Python conventions with Docker patterns.

### Current instructions

| File | Scope | Content |
|------|-------|---------|
| `python.instructions.md` | `stacks/agents/app/**/*.py` | Module design, types, error handling, async, logging |
| `stacks.instructions.md` | `stacks/**` | Compose patterns, port binding, networking |
| `testing.instructions.md` | `**/tests/**,**/*test*` | Test philosophy, AAA structure, naming, what to test |
| `docs.instructions.md` | `docs/**/*.md` | ADR format, writing style, when to write docs |
| `pr-workflow.instructions.md` | `.github/workflows/**` | PR lifecycle, review triggers, merge process |

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

**Output templates** — more reliable than prose descriptions:
```json
{
  "event": "APPROVE",
  "body": "✅ **Approved** — no issues found.\n\n---",
  "comments": []
}
```

### Current skills

| Skill | Invocable | Auto-loaded | Purpose |
|-------|:-:|:-:|---------|
| `auto-implement` | ❌ | ✅ | Orchestrator: implement issues, fix review feedback |
| `code-review` | ❌ | ✅ | Orchestrator: structured PR review with JSON output |

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
