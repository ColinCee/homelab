# ADR-007: Agent Network Isolation

**Date:** 2026-04-09
**Status:** Superseded by [ADR-010](010-agent-security-model.md) (network model unchanged; credential scoping rationale updated)

## Context

The agent container (FastAPI orchestrator + Copilot CLI) runs on Docker's
default bridge with unrestricted outbound access. Earlier planning explored
egress filtering, but the agent architecture evolved: the CLI no longer receives
GitHub API tokens, and the orchestrator is the only component that talks to the
GitHub API or pushes git refs.

This ADR records the current threat model and why unrestricted outbound remains
the accepted choice.

### What the service connects to

| Destination | Used by | Why |
|-------------|---------|-----|
| `api.github.com` | Orchestrator | App auth, read issues/PRs, create PRs, post reviews/comments |
| `api.githubcopilot.com` | Copilot CLI | LLM inference |
| `github.com:443` | Orchestrator | Clone, fetch, push |
| Inbound `100.100.146.119:8585` | GitHub Actions via Tailscale | Trigger `/review` and `/implement` |

## Options Considered

### Option A: Docker `internal: true` network + sidecar proxy

Put the agent on an internal network and force outbound traffic through a proxy.

- ✅ Strongest explicit egress allowlist
- ❌ `internal: true` blocks straightforward port publishing for the inbound API
- ❌ Proxy env vars are advisory; tools can bypass them
- ❌ DNS and allowed-endpoint exfiltration still exist
- ❌ Adds ingress/proxy coordination complexity to a small deployment
- **Verdict:** Rejected. High complexity for a bypassable control.

### Option B: Host firewall egress rules

Restrict the container's outbound access with host-level firewall rules.

- ✅ Enforced below the container
- ❌ GitHub and Copilot endpoints rely on dynamic infrastructure
- ❌ Breaks silently when upstream IPs change
- ❌ Adds operational fragility to the critical automation path
- **Verdict:** Rejected. Too brittle for third-party SaaS endpoints.

### Option C: Split orchestrator and CLI into separate containers

Move the CLI into its own container with tighter network access.

- ✅ Cleaner process separation
- ✅ Could narrow the CLI's direct outbound footprint further
- ❌ The orchestrator still needs GitHub and git network access
- ❌ Shared-volume/session coordination gets more complex
- ❌ The biggest credential-isolation win already exists: the CLI does not get
  GitHub API tokens today
- **Verdict:** Rejected. Marginal security gain for meaningful complexity.

### Option D: Accept unrestricted outbound and rely on existing controls

Keep the current networking model and make credential scoping plus trigger
gating the real boundary.

- ✅ Simple, reliable, and easy to operate
- ✅ Matches the actual threat model better than brittle egress filtering
- ✅ Already paired with scoped credentials, workflow gating, and runtime hardening
- ❌ A compromised process can still reach arbitrary internet hosts
- **Verdict: Chosen.** The added controls target the realistic failure modes more directly.

## Decision

Accept unrestricted outbound network access. The security boundary is:

1. **Credential scoping** — Copilot token only for Copilot Requests; GitHub App
   token only inside the orchestrator.
2. **Trigger gating** — `/review` requires an authorized PR comment; `/implement`
   requires an authorized issue comment or the `agent` label.
3. **Prompt-injection defenses** — implementation only trusts issues from
   trusted roles; review thread context includes only bot-authored comments.
4. **Runtime hardening** — non-root container, dropped capabilities, no new privileges.
5. **No host access** — no Docker socket (see [ADR-011](docs/decisions/011-docker-socket-for-workers.md) for the exception), no host filesystem mounts, no personal credentials.

Even with an egress allowlist, data could still leave through GitHub or other
allowed APIs. The more important controls are who can trigger the agent and what
credentials the agent components actually possess.

## Outcome

Network isolation is closed as won't-fix for now. If the service later gains new
credentials, public ingress, or a materially different runtime shape, revisit
this ADR. Until then, outbound filtering is complexity without a proportional
reduction in risk.

## References

- [ADR-004: Isolated Agent Service](004-isolated-review-agent.md)
- `.github/workflows/code-review.yaml`
- `.github/workflows/implement.yaml`
- `stacks/agents/app/copilot.py`
- `stacks/agents/app/github.py`
