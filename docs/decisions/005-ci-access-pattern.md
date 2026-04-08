# ADR-005: CI Access Pattern — Tailscale Ephemeral + Scoped Curl

**Date:** 2026-04-08
**Status:** Accepted
**Requirements:** R2 (webhook deploy), R22 (credential isolation)

## Context

GitHub Actions workflows need to reach the Beelink to trigger agent operations (code review, deploy). The Beelink is behind Tailscale with no public ingress. We need a secure, reliable way for CI to communicate with it.

## Options Considered

### Option A: SSH from CI to Beelink

Run commands directly on the host via SSH.

- ✅ Simple, well-understood
- ❌ Gives CI shell access to the entire host — massive blast radius
- ❌ SSH key management and rotation complexity
- ❌ Any workflow compromise means full host access
- **Verdict:** Too much privilege. Shell access is far more than CI needs.

### Option B: Cloudflare Tunnel public endpoint

Expose the agent API via Cloudflare Tunnel with auth.

- ✅ No Tailscale dependency in CI
- ❌ Public endpoint for an internal service — unnecessary attack surface
- ❌ Requires Cloudflare auth token management
- ❌ Agent API is admin-only, public exposure violates least privilege
- **Verdict:** Over-exposes an internal service.

### Option C: Tailscale ephemeral node + scoped curl

CI spins up a Tailscale ephemeral node (dies when workflow ends), then curls the agent API on the tailnet.

- ✅ Zero public exposure — agent only reachable within tailnet
- ✅ Minimal privilege — CI can only hit HTTP endpoints, no shell access
- ✅ Ephemeral — node auto-expires, no stale keys
- ✅ Scoped — Tailscale ACLs can restrict which nodes reach which ports
- ✅ Simple — 3 lines in workflow (install, connect, curl)
- ❌ Requires Tailscale auth key as GitHub secret
- **Verdict: Chosen.** Best balance of security and simplicity.

## Decision

Use Tailscale ephemeral nodes in CI workflows. Each workflow run:

1. Installs Tailscale (`tailscale/github-action@v3`)
2. Connects with an ephemeral auth key (`oauth-client-id` + `oauth-secret`)
3. Curls `http://beelink:8585/<endpoint>` on the tailnet
4. Node auto-expires when the workflow ends

The agent API binds to `100.100.146.119:8585` (Tailscale IP only), unreachable from the public internet.

## References

- [Tailscale GitHub Action](https://github.com/tailscale/github-action)
- [ADR-004](004-isolated-review-agent.md) — agent architecture
- `.github/workflows/code-review.yaml` — implementation
