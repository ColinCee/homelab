# ADR-012: Deploy pipeline — CI-to-server transport

**Date:** 2026-04-17
**Status:** Proposed

## Context

We're replacing Dokploy with a GitHub Actions pipeline that detects changed
stacks, generates `.env` files from GitHub secrets + `.env.example` templates,
and runs `docker compose up` on beelink.

The pipeline is built (`deploy.yaml`, `detect-stacks.sh`, `generate-env.sh`,
`deploy.sh`). The open question is how CI reaches beelink to execute the
deploy.

The original design joined a GitHub-hosted runner to the tailnet via Tailscale
OAuth (`tag:ci`), then SSHed to beelink with a restricted deploy key and
forced-command gate (`deploy-gate.sh`). This hit two blockers:

1. **Tailscale SSH intercepts port 22** before sshd sees the connection, so
   `authorized_keys` (and the forced-command gate) are never consulted.
2. **Tailnet membership is the real exposure** — even if SSH is gated, a
   compromised CI runner on the tailnet can reach any service bound to the
   Tailscale IP. The deploy key restriction is irrelevant if the attacker
   pivots from the tailnet node.

This is a public repository, which constrains some options (e.g., self-hosted
runners must not be reachable from fork PR workflows).

## Options Considered

### A: Tailscale SSH `accept` for CI

Add `tag:ci` to the SSH ACL so Tailscale SSH accepts the connection directly.

**Pros:**
- Simple — one ACL change, no sshd config
- No deploy key needed (Tailscale handles auth)

**Cons:**
- CI gets full shell access as `colin` — forced-command gate is bypassed
- Tailnet exposure unchanged — compromised runner has network access to all
  services on beelink
- If Tailscale OAuth credentials leak, attacker has full SSH

**Verdict: Rejected.** Solves the SSH interception problem but doesn't address
the fundamental tailnet exposure concern. Increases blast radius compared to the
current (broken) design.

### B: sshd on a second port (2222) for CI

Configure sshd to listen on port 2222 alongside Tailscale SSH on port 22. CI
SSHes to port 2222 where sshd reads `authorized_keys` and the forced-command
gate works.

**Pros:**
- Forced-command gate works (sshd handles the connection)
- Desktop Tailscale SSH unchanged
- Minimal blast radius for the deploy key itself

**Cons:**
- CI runner still joins the tailnet — network exposure unchanged
- Extra port to manage, document, and firewall
- Added complexity for marginal security gain (the deploy key is restricted,
  but the tailnet membership is not)

**Verdict: Rejected.** Restricts what the deploy key can do, but doesn't
restrict what the tailnet node can reach. The threat model concern is network
position, not SSH command scope.

### C: Disable Tailscale SSH entirely

Turn off Tailscale SSH, use regular sshd for all connections. Desktop manages
SSH keys in `authorized_keys`.

**Pros:**
- Forced-command gate works
- Simpler mental model (one SSH path)

**Cons:**
- CI runner still joins the tailnet
- Desktop loses Tailscale SSH convenience and session audit logging
- Lose Tailscale's automatic key management for trusted devices

**Verdict: Rejected.** Same tailnet exposure as B, plus loses desktop
convenience. All three SSH-based options share the core problem: a CI runner on
the tailnet is a high-value pivot point.

### D: Self-hosted GitHub Actions runner on beelink

Run a GitHub Actions runner directly on beelink. The runner connects **outbound**
to GitHub — no CI machine joins the tailnet. Workflow jobs execute locally.

**Pros:**
- No tailnet exposure — runner initiates outbound HTTPS to GitHub, nothing
  inbound
- No SSH at all — deploy scripts run locally on beelink
- `.env` generation happens locally (no file transfer)
- Removes 3 secrets (DEPLOY_SSH_KEY, TS_OAUTH_CLIENT_ID, TS_OAUTH_SECRET)
  and `deploy-gate.sh`
- GitHub auto-updates the runner binary
- Keeps everything already built: `detect-stacks.sh`, `generate-env.sh`,
  `deploy.sh`

**Cons:**
- Anyone with repo write access can modify workflows to run arbitrary code on
  beelink (inherent to self-hosted runners)
- GitHub warns against self-hosted runners on public repos due to fork PR risk

**Public repo mitigation:** Only `deploy.yaml` uses the self-hosted runner
label (`beelink`), and it only triggers on `push:main` + `workflow_dispatch` —
both require write access. All PR-triggered workflows (`ci.yaml`,
`code-review.yaml`, `implement.yaml`) stay on `ubuntu-24.04`. Fork PRs cannot
reach the self-hosted runner.

| Workflow | Trigger | Runner | Fork PR risk |
|----------|---------|--------|-------------|
| ci.yaml | pull_request | ubuntu-24.04 | None |
| code-review.yaml | issue_comment | ubuntu-24.04 | None |
| implement.yaml | issues | ubuntu-24.04 | None |
| deploy.yaml | push:main, workflow_dispatch | **beelink** | None (can't trigger) |

**Verdict: Recommended.** Eliminates the tailnet exposure problem entirely.
The remaining risk (repo write access = code execution on beelink) is
acceptable for a single-user homelab where collaborator access is controlled.

## Decision

**Option D: Self-hosted runner on beelink.**

Setup:

1. Install GitHub Actions runner on beelink with label `beelink`
2. Run as a systemd service under a dedicated user
3. Change `deploy.yaml`: `runs-on: beelink` (deploy job only)
4. Remove from workflow: Tailscale connect, deploy key setup, SSH steps
5. Deploy step becomes: `generate-env.sh` → `deploy.sh` (both local)
6. Remove secrets: DEPLOY_SSH_KEY, TS_OAUTH_CLIENT_ID, TS_OAUTH_SECRET
7. Remove from repo: `deploy-gate.sh`
8. Remove from beelink: deploy key from `authorized_keys`

What stays unchanged:

- `detect-stacks.sh` — stack change detection
- `generate-env.sh` — `.env` generation from templates
- `deploy.sh` — git pull + docker compose up
- `.env.example` templates — secret variable mapping
- GitHub secrets for app credentials (BOT_APP_ID, etc.)
- `workflow_dispatch` manual deploy UI

## References

- `docs/runbooks/deploy-migration.md` — migration journal with SSH blocker details
- `.github/workflows/deploy.yaml` — current (pre-migration) workflow
- ADR-010: Agent security model
- [GitHub docs: self-hosted runner security](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners#self-hosted-runner-security)
