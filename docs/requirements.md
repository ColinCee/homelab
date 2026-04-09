# Roadmap

What's left to build. Solved items live in their respective [ADRs](decisions/) — this doc tracks only active and planned work.

## Active

| Item | Status | Notes |
|------|--------|-------|
| Autonomous agents — pick up issues and create PRs independently | 🔧 In progress | Implement/fix/review loop merged, needs battle-testing |
| Autonomous issue resolution — label `agent` or `/implement` triggers full cycle | 🔧 In progress | Capped at 3 fix iterations |

## Planned

| Item | Priority | Notes |
|------|----------|-------|
| Staging environment — feature branch deployment to isolated namespace | Should | Avoids deploying over production to test |
| Docker socket hardening | Should | |
| Multi-node scaling (Dokploy Swarm) | Nice | When a second machine is added |

## Scaling Path

| Scale | Tool | When |
|-------|------|------|
| 1 node, <15 services | Dokploy (Docker) | Now |
| 2–5 nodes | Dokploy multi-server (Docker Swarm) | When a second machine is added |
| Outgrow Swarm | K3s | If/when Swarm isn't enough |
