# ADR-007: Agent Network Isolation

**Date:** 2026-04-09
**Status:** Accepted

## Context

The agent container (FastAPI orchestrator + Copilot CLI) runs on the default Docker bridge with unrestricted outbound access. The roadmap previously called for egress filtering — only allowing the agent to reach required APIs. ADR-004 noted this as impractical; this ADR documents the full analysis.

### What the agent connects to

| Destination | Who needs it | Why |
|---|---|---|
| `api.github.com` | Orchestrator (httpx) | Auth, read issues/PRs, post reviews, create PRs |
| `api.github.com` | Copilot CLI via `gh` (review only) | Post reviews with `gh api` |
| `api.githubcopilot.com` | Copilot CLI | LLM inference |
| `github.com:443` | Orchestrator (git) | Clone, fetch, push |
| Inbound `100.100.146.119:8585` | GitHub Actions via Tailscale | Trigger reviews/implements |

The orchestrator and CLI run in the same container. The review CLI receives a `GH_TOKEN` (App token, PR write + contents read). The implement CLI receives no `GH_TOKEN` — it only talks to Copilot API.

## Options Considered

### Option A: Docker `internal: true` network + sidecar proxy

Put the agent on an internal Docker network (no direct outbound) and route traffic through a sidecar proxy (Squid/Envoy) that allowlists specific domains.

- ✅ Strongest isolation — only explicitly allowed traffic leaves
- ❌ `internal: true` networks don't support port publishing — can't receive inbound from Tailscale on `100.100.146.119:8585`
- ❌ Would need a separate ingress container on both networks to forward requests
- ❌ HTTP proxy env vars (`HTTP_PROXY`) are advisory — `git`, `curl`, and the Copilot CLI binary can ignore them
- ❌ DNS exfiltration bypasses HTTP proxies entirely
- ❌ Three containers (agent + proxy + ingress) instead of one
- **Verdict:** Rejected. The port publishing constraint is a hard blocker, and the proxy is bypassable.

### Option B: iptables/nftables egress rules on the host

Add firewall rules to restrict the agent container's outbound to specific IPs/CIDRs for GitHub and Copilot endpoints.

- ✅ Enforced at the kernel level — can't be bypassed from inside the container
- ❌ GitHub API uses dynamic IPs across multiple CDN ranges — requires polling the [GitHub meta API](https://api.github.com/meta) and updating rules
- ❌ Copilot API endpoints aren't published — would need to discover and maintain the IP list
- ❌ Rules break silently when IPs rotate, causing hard-to-debug failures
- ❌ Fragile — ties operational reliability to IP stability of third-party services
- **Verdict:** Rejected. IP-based filtering is too fragile for services with dynamic infrastructure.

### Option C: Split into orchestrator + CLI containers

Separate the orchestrator (FastAPI, credentials, GitHub API access) from the CLI (Copilot binary, minimal network). Communicate via shared volume.

- ✅ CLI container could run on a restricted network (only Copilot API)
- ✅ Credential isolation — CLI never sees the App private key
- ❌ Review CLI still needs `GH_TOKEN` to post reviews via `gh` — still has GitHub API access
- ❌ Doubles container count and operational complexity
- ❌ Shared volume coordination adds failure modes (race conditions, stale state)
- ❌ Marginal gain — the implement CLI already gets no `GH_TOKEN`
- **Verdict:** Rejected. Significant complexity for minimal security gain. The implement path is already credential-isolated.

### Option D: Accept unrestricted outbound, rely on existing controls

Keep the current model. The security boundary is credential scoping and trigger gating, not the network layer.

- ✅ Simple — one container, standard Docker networking
- ✅ Existing mitigations cover the realistic threat model (see below)
- ✅ Matches industry practice (GitHub Copilot, CodeRabbit, Qodo all run without egress filtering)
- ❌ A compromised CLI process could reach any internet host
- **Verdict: Chosen.** The marginal security of egress filtering doesn't justify the complexity or fragility.

## Decision

Accept unrestricted outbound network access. The agent's security model relies on:

1. **Credential scoping** — PAT has only Copilot Requests; App has only PR write + contents read
2. **Trigger gating** — fork PRs blocked; `/review` and `/implement` role-gated to OWNER/MEMBER/COLLABORATOR
3. **Prompt injection defence** — only bot-authored comments included in thread context; issue author role checked
4. **Runtime hardening** — `cap_drop: ALL`, `no-new-privileges`, non-root, resource limits
5. **No host access** — no Docker socket, no host mounts, no personal credentials

Even with egress filtering, data could be exfiltrated through allowed endpoints (create gists, post to issues). The realistic threat is prompt injection via malicious PR/issue content, and the mitigations above address that directly.

### Outcome

Agent network isolation is **closed as won't-fix**. The analysis shows egress filtering is either technically infeasible (port publishing constraint), fragile (IP-based rules), or insufficient (proxy bypass, DNS exfil). The existing controls provide the actual security boundary.

## References

- [ADR-004: Isolated Code Review Agent](004-isolated-review-agent.md) — original analysis of egress proxy
- [GitHub meta API](https://api.github.com/meta) — dynamic IP ranges for GitHub services
- [Docker networking: internal](https://docs.docker.com/reference/compose-file/networks/#internal) — `internal: true` constraint
