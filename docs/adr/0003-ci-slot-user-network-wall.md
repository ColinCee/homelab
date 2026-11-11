# 3. A firewall rule, not a namespace per VM, walls off an escaped CI VM

## Status

Accepted.

## Context

Each CI job runs in a Firecracker microVM under the jailer, as its slot's own user (`fc-slotN`), chrooted, in Beelink's network namespace ([CI runner](../runbooks/ci-runner.md)). `homelab-ci.nft` filters what the VM's tap may reach, but a process that escaped the VM runs on the host as the slot user. Measured before this change: `fc-slot0` could open connections to a loopback listener, OpenObserve on the Tailscale address, the router on the LAN and the internet. Escaped build sandboxes are how hosted CI platforms have been breached.

Two designs close this:

- A network namespace per VM: the tap, NAT and rules move into each namespace. Complete, but it rewrites the network layout of a runner in daily use.
- One nftables rule: drop every packet from a socket owned by a slot user. Firecracker opens no IP sockets (its tap carries the VM's traffic, and a tap is not a socket), so nothing legitimate is caught.

## Decision

The rule. `homelab-ci.nft` drops output from any uid in the `slot_uids` set, and `setup-net.sh` fills that set from every `fc-slot*` user on every load. `run-vm.sh` refuses to start a VM whose slot user is not in the set. `audit.sh` checks that a slot user cannot connect while root can, and that every slot user is in the set.

## Consequences

- An escapee cannot connect anywhere or answer a connection: loopback, LAN, tailnet or internet. It can still bind a port and receive packets.
- It still sees Beelink's interfaces and listening ports through netlink, can reach any abstract unix socket (for example multipathd's and iscsiadm's, both root-owned services), and can write to Firecracker's inherited journal stream.
- A namespace per VM remains the way to close those, if an escape ever looks likely enough to justify it.
