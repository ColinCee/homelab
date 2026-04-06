# ADR-002: Repo Tooling — mise + uv + Python

**Date:** 2026-04-06
**Status:** Accepted

## Context

The homelab repo needed a task runner, dependency management, and dev tooling. The repo is infrastructure-as-code (compose files, bootstrap scripts) plus Python scripts for health checks and security audits.

## Decision

### Task runner: mise

**mise** orchestrates everything — tool version management and task running in one tool. Already installed on the server.

Considered and rejected:
- **Make** — ubiquitous but arcane syntax, no tool version management
- **just** — better syntax than Make, but no tool versioning
- **Taskfile (go-task)** — good, but mise handles both tasks and tools
- **nx / turborepo** — designed for JS monorepos with build graphs, wrong abstraction for infra
- **Ansible / Terraform** — overkill for single-node, mise tasks are simpler and more transparent

### Python tooling: uv + ruff + ty + pytest

**uv** for package management (fast, lockfile, replaces pip/venv/pip-tools).
**ruff** for linting and formatting (replaces black + flake8 + isort).
**ty** for type checking (Astral ecosystem, same team as ruff/uv).
**pydantic** for structured output models.

Considered and rejected:
- **Rust** — great language, but subprocess wrappers don't benefit from Rust's strengths; Python is more productive for infra scripting
- **Go** — similar argument; the scripts are thin wrappers around system commands
- **Poetry / PDM** — uv is faster and simpler, from the Astral ecosystem

### Additional dev tools

- **shellcheck** — lints bash in git hooks
- **actionlint** — catches GitHub Actions bugs locally
- **yamllint** — validates YAML (compose files, CI workflows)
- **trivy** — scans Docker images for CVEs
- **Renovate** — auto-PRs when Docker images have updates

### Code structure: bash glue + Python logic

Simple system commands (docker compose up, ufw enable) stay as bash in mise tasks. Logic-heavy tasks (health checks with retries, security audits with structured output) are Python with pydantic models and pytest tests.

## Consequences

- `mise install` sets up all tools on any machine
- `mise run ci` validates everything locally before push
- `mise run check:health` and `mise run check:security` give structured audit reports
- `mise run setup` bootstraps a fresh server
- Python scripts are testable (24 tests with mocked subprocesses)
- Adding new stacks or checks follows established patterns
