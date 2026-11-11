# 5. One nftables table walls Docker's bridge containers off the home network

## Status

Accepted.

## Context

The flight tracker's backend is public through Cloudflare Tunnel, and its image comes from a repository with outside collaborators. Measured from its network namespace before this change: it opened connections to OpenObserve and the DNS server on the Tailscale address (`:5080`, `:5081`, `:53`), Tailscale's DNS (`100.100.100.100`) and the router's admin pages (`192.168.1.1:80`, `:443`). A connection to a tailnet peer leaves as Beelink's own tailnet address. UFW does not stop these: Docker's DNAT turns a published port into forwarded traffic, and routed traffic to the LAN and tailnet is forwarded too.

A second hole: Docker's DNAT matches the Tailscale address from any interface. A machine on the LAN with a route to `100.100.146.119` via Beelink reached the published ports (shown with a stand-in LAN namespace, `ci-runner/tests/test-wall.sh`), bypassing the tailnet policy.

Two places for the rules:

- `DOCKER-USER`, like the CI accepts. It runs after Docker's DNAT, so a container's connection to a published port already looks like one to another container, and it exists only once Docker has started.
- An nftables table of its own, hooked before Docker's DNAT. A drop there is final whatever Docker or UFW accept, and Docker's restarts never touch it.

## Decision

The table, `inet homelab_wall` in `ci-runner/net/homelab-ci.nft`, loaded with the CI table by `setup-net.sh`, so a merge that changes it reaches the host within minutes (`update.sh`). A new connection from a Docker bridge is dropped when it goes to one of Beelink's own addresses, to a private or tailnet range that is not a Docker network, or to the public address. The one exception is DNS to Beelink's own DNS servers (the router today): image builds and containers on Docker's default bridge ask them directly. In the forward hook, a new connection is dropped unless it arrives from a Docker bridge, a CI tap or `tailscale0`.

Before it went live, the same matches logged for an hour with today's stacks and the trial Prometheus stack running: no container used any of these routes. A review then found the DNS route, which only builds and default-bridge containers use.

## Consequences

- A bridge container reaches the internet and other containers, nothing else of the home. One that needs the host must use host networking, as the probes and the `.internal` proxy do.
- A port published on the LAN address would not be reachable from the LAN either; admin ports bind to the Tailscale address anyway.
- The wall loads with `homelab-ci-net.service`, after Docker, so at boot containers run unwalled for the seconds in between.
- The router's DNS server stays reachable from containers, so one can still look up home names and attack the DNS server itself.
- The public address is the one found when the rules last loaded or a CI VM last started; after it changes, containers can reach it until then.
