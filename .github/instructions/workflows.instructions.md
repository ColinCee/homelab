---
applyTo: ".github/workflows/**"
---

# Pull Request & Code Review Workflow

This repo has an automated AI code review system powered by GPT-5.4 running
on a self-hosted agent (Beelink server via Tailscale).

## How it works

1. `code-review.yaml` triggers on `opened` and `ready_for_review` PR events
2. The workflow connects via Tailscale to `beelink:8585/review`
3. The agent fetches the diff, sends it to GPT-5.4 via the Copilot API
4. The agent also fetches any previous bot reviews and includes them in the
   prompt so the model knows what it already flagged
5. The response is structured JSON — the Action posts it as a GitHub review
   from `github-actions[bot]`

## Review cycle for contributors

1. **Create PRs as drafts** — `gh pr create --draft`
2. Push commits, iterate until ready
3. Mark ready: `gh pr ready <number>` → auto-triggers first review
4. Fix any 🔧 Must Fix or 🔒 Security findings
5. Comment `/review` to re-trigger — the model checks if previous issues
   are resolved and only flags remaining or new problems
6. Repeat until APPROVE
7. Repo owner merges — **never merge PRs yourself**

## Review verdicts

- **APPROVE** — no issues, PR can merge
- **REQUEST_CHANGES** — must-fix or security issues block merge
- **COMMENT** — nitpicks only, doesn't block

## Trigger rules

- `opened` / `ready_for_review` — automatic first review
- **No `synchronize` trigger** — pushes don't auto-review (saves premium
  requests). Use `/review` when ready for re-review.
- `/review` comment — manual re-trigger (OWNER/MEMBER/COLLABORATOR only)

## Security gates

- Fork PRs blocked from `pull_request` events via `head.repo.full_name` check
- Fork PRs blocked from `/review` via API step that fetches PR head repo
- `/review` restricted to OWNER/MEMBER/COLLABORATOR `author_association`

## Branch protection

- 1 approving review required (from `github-actions[bot]`)
- CI (`check` job) must pass
- Stale reviews dismissed on new pushes
- Never use `--admin` to bypass branch protection
