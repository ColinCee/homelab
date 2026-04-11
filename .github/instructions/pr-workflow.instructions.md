This instruction applies to files matching the pattern: .github/workflows/**,stacks/agents/**

---
applyTo: ".github/workflows/**,stacks/agents/**"
---

# Pull Request Workflow

This repo has an automated AI code review and implementation system powered by `colins-homelab-bot[bot]`.

## Creating PRs

1. **Create PRs as drafts** — `gh pr create --draft`
2. Push commits, iterate until ready
3. Mark ready: `gh pr ready <number>` → triggers first AI review

## Review cycle

1. When a PR is opened or marked ready, `code-review.yaml` auto-triggers
2. The bot posts a structured review with a verdict:
   - **APPROVE** — no issues found
   - **REQUEST_CHANGES** — blocker-severity issues found
   - **COMMENT** — suggestions only, non-blocking
3. Inline comments use severity tags:
   - 🚫 **Blocker** — bugs, security, breaking changes
   - 💡 **Suggestion** — non-blocking improvement, author decides
   - ❓ **Question** — seeks clarification, non-blocking
4. Fix legitimate findings, push new commits
5. Comment `/review` to re-trigger — the bot receives all unresolved
   review threads and checks if issues were addressed
6. Repeat until you're confident the code is ready — bot reviews are
   **advisory**, not blocking. Use your judgement: fix real issues,
   dismiss false positives
7. Merge when CI passes and you're satisfied with the code

## Important

- **Bot reviews are advisory** — they inform but don't gate merges.
  The bot may produce false positives. If you're confident a finding
  is wrong, explain why in a comment and move on.
- **No `synchronize` trigger** — pushes don't auto-review. Use `/review`.
- Stale reviews are dismissed on new pushes
- Never use `--admin` to bypass branch protection
- Fork PRs are blocked from triggering reviews
