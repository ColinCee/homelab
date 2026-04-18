# Roadmap

What's left to build. Solved items live in their respective [ADRs](decisions/) — this doc tracks only active and planned work.

## Active

| Item | Status | Notes |
|------|--------|-------|
| CLI autonomy — CLI owns full lifecycle (commit, push, PR, merge) | 🔧 In progress | ADR-010 accepted; orchestrator slimmed to thin dispatcher ([#57](https://github.com/ColinCee/homelab/issues/57)) |

## Planned

| Item | Priority | Notes |
|------|----------|-------|
| Staging environment — feature branch deployment to isolated namespace | Should | Avoids deploying over production to test |
| Docker socket hardening | Should | |
| Decommission Dokploy | Should | Runner-based pipeline replaces it (ADR-012); stop Dokploy containers, reclaim ~350 MB RAM |

## Known Limitations — Autonomous Agents

Discovered during implementation and battle-testing. Each is tracked as an issue.

| Limitation | Issue | Impact |
|-----------|-------|--------|
| Can't rebase or resolve merge conflicts | [#41](https://github.com/ColinCee/homelab/issues/41) | PRs stall when `main` moves during implementation |
| Failed runs discard all CLI work | [#42](https://github.com/ColinCee/homelab/issues/42) | ~6 min + 1 premium request wasted per failed attempt |
| Parallel agents can conflict on shared files | [#43](https://github.com/ColinCee/homelab/issues/43) | Low risk today (single agent), blocks scaling |
| Can't self-review review infrastructure changes | — | PRs that change the review skill or orchestrator break the review loop |
