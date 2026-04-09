# ADR-006: Dokploy GitOps — Auto-Deploy with UI Environment Variables

**Date:** 2026-04-08
**Status:** Accepted

## Context

The homelab needs GitOps: push to `main` should auto-deploy all services. Dokploy manages container lifecycle and can auto-deploy from a Git repo. The challenge is handling secrets — compose files need environment variables for credentials (GitHub App ID, tokens, etc.) that can't be committed to the repo.

## Options Considered

### Option A: CI SSH + `docker compose up`

CI SSHes into the Beelink and runs compose commands directly.

- ✅ Simple mental model
- ❌ CI has shell access (see [ADR-005](005-ci-access-pattern.md))
- ❌ Bypasses Dokploy — two deployment paths cause drift
- ❌ Secret management falls on CI (GitHub secrets → SSH → env vars)
- **Verdict:** Too much privilege, duplicates Dokploy's job.

### Option B: `env_file` in compose pointing to host path

Compose files reference `env_file: /home/colin/secrets/agents.env` for secrets.

- ✅ Secrets stay on disk, out of git
- ❌ `env_file` is read by the `docker compose` CLI, not the container runtime
- ❌ Dokploy runs `docker compose` inside its own container — host paths don't exist there
- ❌ CI validation also fails unless the file exists on the runner
- **Verdict:** Doesn't work with Dokploy's architecture.

### Option C: Compose interpolation (`${VAR:?}`) + Dokploy UI env vars

Compose files use `${VAR:?}` syntax (fail-fast if unset). Dokploy provides values via its UI, which writes them to `.env` next to the compose file. Docker Compose reads `.env` automatically.

- ✅ Compose file declares WHAT vars are needed, platform provides VALUES
- ✅ Standard GitOps pattern (same as K8s Sealed Secrets, ArgoCD + Vault)
- ✅ `docker compose config` validates the file — `.env.example` provides dummy values for CI
- ✅ Works with Dokploy's `env -i` deploy command (`.env` file survives since compose reads from disk)
- ❌ Secrets must be set in Dokploy UI (not in git) — requires manual setup per service
- **Verdict: Chosen.** Correct separation of concerns.

## Decision

Use compose variable interpolation with `${VAR:?}` and provide values through Dokploy's UI environment variables.

### How it works

1. Compose files use `${GITHUB_APP_ID:?}` — fails fast if the variable is missing
2. Dokploy writes UI env vars to `.env` next to the compose file before running `docker compose up`
3. Docker Compose reads `.env` from the compose file's directory automatically
4. For CI validation, `.env.example` files provide dummy values (copied to `.env` before `docker compose config`)

### Key details

- **Dokploy deploy command:** `env -i PATH="$PATH" docker compose -p <name> -f ./stacks/<service>/compose.yaml up -d --build`
- `env -i` clears the shell environment, but `.env` survives because compose reads it from disk, not from the shell
- Docker Compose project directory = directory of the first `-f` compose file = where it looks for `.env`
- Volume bind mounts (e.g., PEM files) resolve on the Docker host, so host paths in `volumes:` work fine even when compose runs inside Dokploy's container

### Adding secrets to a new stack

1. Add `${VAR:?}` in `compose.yaml`
2. Add dummy values to `.env.example` (for CI validation)
3. Set real values in Dokploy UI → Environment → compose file's service
4. Redeploy

## References

- [Dokploy compose documentation](https://docs.dokploy.com/docs/core/docker-compose)
- [Docker Compose env_file vs interpolation](https://docs.docker.com/compose/how-tos/environment-variables/)
- `stacks/agents/compose.yaml` — implementation
- `stacks/agents/.env.example` — CI validation pattern
