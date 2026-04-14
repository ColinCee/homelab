# ADR-011: Docker socket for ephemeral worker containers

**Date:** 2025-07-25
**Status:** Accepted

## Context

The agent API container runs CLI tasks (review, implement) as child processes.
When Dokploy redeploys the container (triggered by any push to main — including
the agent's own merges), in-flight CLI work is killed. These sessions run 5–30
minutes and consume premium Copilot tokens that cannot be recovered.

To decouple CLI work from the API lifecycle, we want the API to spawn
**ephemeral worker containers** via Docker. Workers run the same image with a
different entrypoint (`python -m worker`), execute the full CLI lifecycle, and
exit. The API becomes a thin dispatcher: validate trust → spawn worker → monitor
completion.

This requires the API container to have access to the Docker daemon on the host.

## Options Considered

### A: Docker socket mount (`/var/run/docker.sock`)

Mount the host's Docker socket into the API container. The API uses the `docker`
CLI to spawn, monitor, and stop worker containers.

**Pros:**
- Zero additional infrastructure — uses the existing Docker daemon
- Simple to implement — subprocess calls to `docker run/wait/stop/rm`
- Workers are fully isolated from API lifecycle (survive restarts)
- Same operational model as Dokploy itself (which also uses the socket)

**Cons:**
- Grants the API container host-level Docker access (can create any container)
- A compromised API process could escape to the host via container creation

**Verdict: Chosen.** The blast radius argument is decisive — the API already
holds the GitHub App private key and generates installation tokens with full repo
write access. Compromising the API already means full repository control. Adding
Docker socket access expands the blast radius from "repo compromise" to "host
compromise", but on a single-user homelab behind Tailscale, the incremental
risk is minimal.

### B: Docker socket proxy (Tecnativa/docker-socket-proxy)

Run a filtering proxy that restricts which Docker API endpoints the container
can reach.

**Pros:**
- Reduces blast radius by limiting API surface
- Well-known pattern in the Docker ecosystem

**Cons:**
- Cannot restrict to "create containers only" — the proxy's granularity is
  per-API-section (containers: yes/no), not per-operation (create vs delete)
- Adds another service to manage and monitor
- Adds latency and a failure point

**Verdict: Rejected.** The proxy's coarse granularity doesn't meaningfully reduce
risk for our threat model. We'd still be granting broad container API access.

### C: Separate scheduler service

Run a dedicated worker scheduler (e.g., a sidecar) that receives work requests
over HTTP and manages containers itself, keeping the socket out of the API.

**Pros:**
- API never touches Docker — clean separation
- Could enforce resource quotas centrally

**Cons:**
- Significant added complexity for a single-user homelab
- Two services to deploy, monitor, and debug
- The scheduler itself needs the Docker socket — same trust boundary

**Verdict: Rejected.** Moves the trust boundary without reducing it, while
adding operational complexity.

## Decision

Mount the Docker socket into the API container. Mitigations:

1. **Existing container hardening applies:** `no-new-privileges`, `cap_drop: ALL`,
   only `CHOWN`/`FOWNER`/`SETUID`/`SETGID` capabilities added back.
2. **Worker containers inherit the same security profile:** resource limits
   (2 GB RAM, 2 CPUs), no additional capabilities, no socket mount.
3. **Network boundary:** API is only reachable via Tailscale (CGNAT IP binding).
   No public ingress to the agent service.
4. **Socket access is scoped to the agent user** via Docker group membership in
   the entrypoint, not by running as root.

## References

- ADR-010: Agent security model
- ADR-007: Agent network isolation
- Issue #90: Decouple agent CLI work from API container lifecycle
