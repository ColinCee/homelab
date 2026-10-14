# CI runner (Firecracker)

Status: being built, see issue 406. Nothing is registered with GitHub yet.

CI for private repositories runs on Beelink, each job in its own throwaway
Firecracker microVM. Public repositories keep using GitHub-hosted runners, and
the deploy runner is separate (see [Deploying services](deploying-services.md)).

## Design

- **One VM per job, never reused.** Each job boots a private copy of the root
  filesystem; the copy and everything the job wrote are deleted afterwards.
- **Jobs have the internet, not the home network.** VMs reach the internet
  through NAT. Beelink itself, the LAN, the tailnet, Docker networks, the
  other slots and the home's public address (the router answers on it from
  inside) are dropped. A VM may only use its own address, and nothing, Beelink
  included, can open a connection to a VM. The iptables accept is limited to
  the uplink, so it opens nothing private even without the nftables table.
- **Firecracker is jailed.** The jailer runs it as the slot's own user
  (`fc-slotN`), chrooted, with Firecracker's syscall filter, at a lower CPU
  weight than the services and with a memory cap.
- **One VM per slot.** A lock stops two runs sharing a slot; if a run dies,
  the next one kills whatever it left behind.
- **No credentials in the VM.** A VM learns its task from Firecracker's metadata
  service (MMDS); the host keeps the GitHub credential.
- **Installed by hand.** Everything lives root-owned under
  `/usr/local/lib/homelab-ci` and `/var/lib/homelab-ci`. Deploys never touch
  it, so a push to `main` cannot change it.

| Piece | Where |
|-------|-------|
| Versions, release hash | [`ci-runner/versions.env`](../../ci-runner/versions.env) |
| Host setup | [`ci-runner/install-host.sh`](../../ci-runner/install-host.sh) |
| Network rules | [`ci-runner/net/`](../../ci-runner/net/), table `inet homelab_ci`, `homelab-ci-net.service` |
| VM image | [`ci-runner/rootfs/`](../../ci-runner/rootfs/), built by [`build-rootfs.sh`](../../ci-runner/build-rootfs.sh) |
| Boot one VM | [`ci-runner/run-vm.sh`](../../ci-runner/run-vm.sh) |

Each slot `N` has tap `fc-tapN`, host `10.200.N.1`, guest `10.200.N.2`. Two
slots of 2 vCPU and 2 GiB each.

## Operating

From a checkout of `main` on Beelink:

```bash
sudo ci-runner/install-host.sh       # Firecracker, slot users, network rules
sudo ci-runner/build-rootfs.sh       # VM image and kernel
sudo ci-runner/tests/test-network.sh # prove the network rules from inside a VM
```

Re-run `build-rootfs.sh` after a kernel upgrade, since the VMs boot the host's
kernel. After upgrading Firecracker (Renovate opens the PR; put the new release
hash in `versions.env` by hand), re-run `install-host.sh` and the network test.

The network test boots a VM that must reach GitHub, PyPI and npm, must time out
on every private address and the public address, and must not get out with
another address. It then connects to the VM from a container and from Beelink
and checks that the drop rule counted it. Every check must pass. Run it after
any change to the rules, Docker or UFW.

## Review

Re-run the network test and the [security audit](security-audit.md) checks
every quarter, and after every Firecracker, kernel or runner upgrade.
