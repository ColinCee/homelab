---
applyTo: "docs/**/*.md"
---

# Documentation Conventions

Plain markdown, viewable in GitHub or as an Obsidian vault. Encrypted private docs use git-crypt.

## Structure

- `docs/requirements.md` — living north-star document with problems (P1–P8) and requirements (R1–R20) in tables
- `docs/decisions/` — append-only ADRs numbered sequentially (e.g., `001-dokploy.md`)
- `docs/runbooks/` — operational how-tos with commands you can copy-paste
- `docs/private/` — encrypted sensitive docs (security audits, network topology)

## ADR Format

ADRs follow this structure — keep it consistent:

```markdown
# ADR-NNN: Title

**Date:** YYYY-MM-DD
**Status:** Accepted | Superseded by ADR-NNN
**Requirements:** R1, R5, R12

## Context
## Options Considered
### Option A (with pros/cons/verdict for each)
## Decision
## References
```

- Always link back to requirement IDs from `requirements.md`
- Include a feature comparison table when evaluating multiple options
- Verdicts should be one sentence explaining why it was chosen or rejected

## Writing Style

- Direct and concise — write for a future version of yourself debugging at 2am
- Use tables for comparisons and status tracking
- Include copy-pasteable commands in runbooks
- Don't duplicate information — link to other docs or the README instead
