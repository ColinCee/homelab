# CI runner (Firecracker)

Live since 2026-10-14, serving cortex. A push reached a running job in about
14 seconds; the first cortex job took 38 seconds.

CI for private repositories runs on Beelink, each job in its own throwaway
Firecracker microVM. Public repositories keep using GitHub-hosted runners, and
the deploy runner is separate (see [Deploying services](deploying-services.md)).

## Using it

A job asks for the runner by its label:

```yaml
jobs:
  check:
    runs-on: [self-hosted, linux, x64, homelab-fc]
```

The VM is Ubuntu 24.04 with the GitHub runner, git, build tools and passwordless
`sudo`, with Node 22, uv and Python 3.15 already installed (pinned in
`versions.env`): `actions/setup-node` finds Node in the runner's tool cache and
uv finds Python, so neither downloads. Package caches go through GitHub's
cache, which keeps a branch from writing the entries `main` reads; turn it on
with `enable-cache: true` on `astral-sh/setup-uv` (it is off by default on
self-hosted runners) and `cache: npm` on `actions/setup-node`.

A job running alone gets 4 vCPUs and 4 GiB; when both slots are busy, the
second gets 2 vCPUs and 2 GiB. A job that needs more memory loses the greedy
process, not the VM.

Only repositories listed on Beelink in `/etc/homelab-ci/repos` (root-only,
`owner/name` per line) are served, and only while they are private: privacy is
checked on every poll, and once a repo is public no new VM starts for it and its
idle ones are stopped. Before making a repo public, move its workflows to
GitHub-hosted runners and take it off the list and the token.

## Design

- **One VM per job, never reused.** The supervisor polls GitHub every 2
  seconds (unchanged answers are free) for queued jobs with the label and boots a VM for each, up to two at
  once. The VM's root filesystem is a private copy, deleted with everything the
  job wrote. However the job ends, the VM ends, within seconds. A VM left
  without a job (cancelled before it started, or taken by another runner) is
  stopped about 30 to 40 seconds after it boots.
- **One VM waits ready.** A standby VM is kept booted and registered for the
  first repo in `/etc/homelab-ci/repos`, so a job there starts in about a
  second instead of waiting for a VM to boot and its runner to start (about 8
  seconds). It costs about 450 MB of RAM while idle, and always has 4 vCPUs. Once it takes a job it is
  an ordinary job VM and a new standby boots in the free slot; an idle standby
  is replaced every 45 minutes, and stopped if its repo stops being private.
- **Just-in-time registration.** For each VM the supervisor asks GitHub for a
  runner registration that is good for exactly one job and passes only that to
  the VM. The GitHub token never leaves Beelink.
- **Jobs have the internet, not the home network.** VMs reach the internet
  through NAT. Beelink itself, the LAN, the tailnet, Docker networks, the
  other slots and the home's public address (the router answers on it from
  inside) are dropped. A VM may only use its own address, and nothing, Beelink
  included, can open a connection to a VM. The iptables accept is limited to
  the uplink, so it opens nothing private even without the nftables table.
- **Firecracker is jailed.** The jailer runs it as the slot's own user
  (`fc-slotN`), chrooted, with Firecracker's syscall filter, no capabilities,
  at a lower CPU weight than the services and with a memory cap.
- **One VM per slot.** A lock stops two runs sharing a slot; if a run dies,
  the next one kills whatever it left behind.
- **Installed by hand.** Everything lives root-owned under
  `/usr/local/lib/homelab-ci`, `/var/lib/homelab-ci` and `/etc/homelab-ci`.
  Deploys never touch it, so a push to `main` cannot change it.

| Piece | Where |
|-------|-------|
| Versions, release hashes | [`ci-runner/versions.env`](../../ci-runner/versions.env) |
| Host setup | [`ci-runner/install-host.sh`](../../ci-runner/install-host.sh) |
| Network rules | [`ci-runner/net/`](../../ci-runner/net/), table `inet homelab_ci`, `homelab-ci-net.service` |
| VM image | [`ci-runner/rootfs/`](../../ci-runner/rootfs/), built by [`build-rootfs.sh`](../../ci-runner/build-rootfs.sh) |
| Boot one VM | [`ci-runner/run-vm.sh`](../../ci-runner/run-vm.sh) |
| Supervisor | [`scripts/ci_supervisor.py`](../../scripts/ci_supervisor.py), `homelab-ci-supervisor.service` |
| Checks | [`ci-runner/tests/`](../../ci-runner/tests/): `audit.sh`, `test-network.sh`, `test-load.sh` |

Each slot `N` has tap `fc-tapN`, host `10.200.N.1`, guest `10.200.N.2`.

## Operating

From a checkout of `main` on Beelink:

```bash
sudo ci-runner/install-host.sh   # Firecracker, slot users, network, supervisor files
sudo ci-runner/build-rootfs.sh   # VM image and kernel
sudo ci-runner/tests/audit.sh    # must pass before the supervisor runs
sudo systemctl enable --now homelab-ci-supervisor.service
journalctl -u homelab-ci-supervisor -f
```

The GitHub token is a fine-grained token limited to the served repositories,
with Administration read and write (to create one-job registrations) and Actions
read. Store it without it reaching the screen or shell history:

```bash
bash -c 'read -rsp "Token: " t; echo; printf "%s" "$t" | sudo tee /etc/homelab-ci/github-token >/dev/null; sudo chmod 600 /etc/homelab-ci/github-token'
```

To serve another repository, add it to `/etc/homelab-ci/repos` and to the
token's repository access, then restart the supervisor.

Re-run `build-rootfs.sh` after a kernel upgrade, since the VMs boot the host's
kernel, and after a runner upgrade. For a Firecracker or runner upgrade,
Renovate opens the PR; put the new release hash in `versions.env` by hand. Then
re-run `install-host.sh`, `build-rootfs.sh` and the audit.

## Checks

- **`audit.sh`** is the gate. It checks host settings, what a slot user could
  read or write after escaping a VM (the token, Docker, Colin's home, the
  image), that the image holds no keys or tokens, how a live Firecracker is
  confined, that a VM is told only its task, and runs the network test.
- **`test-network.sh`** boots a VM that must reach GitHub, PyPI and npm, must
  time out on every private address and the public address, and must not get
  out with another address. It then connects to the VM from a container and
  from Beelink and checks that the drop rule counted it. Every check must pass.
- **`test-load.sh`** runs a VM in every slot at full CPU and most of its memory
  while timing Home Assistant and Grafana. In October 2026 their worst response
  went from 1 ms to 4 ms.

## Review

Every quarter, and after every Firecracker, kernel or runner upgrade: run
`audit.sh`, and the [security audit](security-audit.md) checks.
