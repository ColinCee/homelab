# Roadmap

What's left to build. Solved items live in their respective [ADRs](decisions/) — this doc tracks only active and planned work.

## Active

| ID | Problem / Requirement | Status | Notes |
|----|----------------------|--------|-------|
| P10 | Autonomous agents — agents pick up issues and create PRs independently | 🔧 In progress | Implement/fix/review loop merged, needs battle-testing |
| R24 | Autonomous issue resolution — label `agent` or `/implement` triggers full cycle | 🔧 In progress | Capped at 3 fix iterations |

## Planned

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| P12 / R27 | Staging environment — feature branch deployment to isolated namespace | Should | Avoids deploying over production to test |
| R16 | Docker socket hardening | Should | |
| R19 | Multi-node scaling (Dokploy Swarm) | Nice | When a second machine is added |
| R23 | ~~Agent network isolation~~ | ~~Should~~ | Won't-fix — [ADR-007](decisions/007-agent-network-isolation.md) |

## Scaling Path

| Scale | Tool | When |
|-------|------|------|
| 1 node, <15 services | Dokploy (Docker) | Now |
| 2–5 nodes | Dokploy multi-server (Docker Swarm) | When a second machine is added |
| Outgrow Swarm | K3s | If/when Swarm isn't enough |
