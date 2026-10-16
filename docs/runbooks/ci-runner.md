# CI runner (Firecracker)

Live since 2026-10-14, serving cortex. A cortex run goes from created to done
in about 20 seconds, against 26 to 29 on GitHub's runner for the same commit.

How fast it is, against other sandboxes and GitHub's runners: [CI runner performance](../ci-runner-performance.md).

CI for private repositories runs on the homelab's server, each job in its own throwaway
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
`sudo`, with Node 24 LTS, uv and Python 3.15 already installed (pinned in
`versions.env`) and on the job's `PATH`, so a job needs no setup actions. The
actions the CI repos use (`CACHED_ACTIONS` in `versions.env`) are unpacked in
the image, so "Set up job" does not download them; a tag that has moved on is
downloaded as usual until the list is refreshed and the image rebuilt.

The fastest shape for a job, as cortex's `.github/workflows/linux.yml` does it:
checkout, then one step that runs `uv sync --locked --no-install-project` and
copies `node_modules` from the cache disk when `package-lock.json` matches the
one beside it (falling back to `npm ci`), then the tests. Measured results are
in [CI runner performance](../ci-runner-performance.md).

### Cache disk

Each VM gets its repo's **cache disk** at `/cache`: uv's and npm's caches and
the Python environment from the repo's last green push to its default branch.
Each VM gets its own copy (about 0.5 s, before boot, like the root disk), so a
job can install into it but nothing it writes outlives the VM, and a branch can
never change what the next job starts from. The VM points `UV_CACHE_DIR` (`/cache/uv`),
`npm_config_cache` (`/cache/npm`) and `UV_PROJECT_ENVIRONMENT` (`/cache/venv`)
at it, so `uv sync` and `npm ci` find almost everything in place.

How it is made, without the host running any of the repo's code:

1. A green push to the default branch tars `/cache` and uploads it as the
   artifact `homelab-fc-cache` (the job's last two steps; see cortex's
   `.github/workflows/linux.yml`).
2. The supervisor checks for a newer one every minute, takes it only if its run
   was a successful push to the default branch, unpacks it (plain files,
   folders and links only, nothing outside the folder) and builds
   `/var/lib/homelab-ci/caches/<owner>__<repo>.ext4`, root-owned and read-only
   to others. VMs started after that use it.

A job should not restore GitHub's caches as well. Without a cache disk yet,
`/cache` starts empty and the job downloads as usual. The default branch's job
also puts `node_modules` and its `package-lock.json` on the disk (the tar step
names them), copied rather than linked so TypeScript's incremental build info
stays valid.

A job running alone gets 4 vCPUs and 4 GiB; when both slots are busy, the
second gets 2 vCPUs and 2 GiB. A job that needs more memory loses the greedy
process, not the VM.

Only repositories listed in the host's root-only repo list (`REPOS_FILE` in the supervisor,
`owner/name` per line) are served, and only while they are private: privacy is
checked on every poll, and once a repo is public no new VM starts for it and its
idle ones are stopped. Before making a repo public, move its workflows to
GitHub-hosted runners and take it off the list and the token.

## Design

- **One VM per job, never reused.** The supervisor polls GitHub every 2
  seconds (unchanged answers are free) for queued jobs with the label and boots
  a VM for each, up to two at once. The VM's root filesystem is a private copy, deleted with everything the
  job wrote. However the job ends, the VM ends, within seconds. A VM left
  without a job (cancelled before it started, or taken by another runner) is
  stopped about 30 to 40 seconds after it boots.
- **One VM waits ready.** A standby VM is kept booted and registered for the
  first repo in that list, so a job there starts in about a
  second instead of waiting for the next poll, a VM to boot and its runner to start
  (about 11 seconds). It costs about 450 MB of RAM while idle, and always has 4 vCPUs.
  Once it takes a job it is an ordinary job VM and a new standby boots in the
  free slot; an idle standby is replaced every 45 minutes, and stopped if its
  repo stops being private. `/etc/homelab-ci/standbys` sets how many wait (1 by
  default); 2 was measured and not worth its RAM (see the performance doc).
- **Just-in-time registration.** For each VM the supervisor asks GitHub for a
  runner registration that is good for exactly one job and passes only that to
  the VM. The GitHub token never leaves the host.
- **Jobs have the internet, not the home network.** VMs reach the internet
  through NAT. The host itself, the LAN, the tailnet, Docker networks, the
  other slots and the home's public address (the router answers on it from
  inside) are dropped. A VM may only use its own address, and nothing, the host
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

Each slot has its own tap device and a private address pair of its own.

## Operating

From a checkout of `main` on the host:

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
bash -c 'read -rsp "Token: " t; echo; printf "%s" "$t" | sudo tee "$TOKEN_FILE" >/dev/null; sudo chmod 600 "$TOKEN_FILE"'  # TOKEN_FILE: see the supervisor
```

To serve another repository, add it to the repo list and to the
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
  from the host and checks that the drop rule counted it. Every check must pass.
- **`test-load.sh`** runs a VM in every slot at full CPU and most of its memory
  while timing Home Assistant and Grafana. In October 2026 their worst response
  went from 1 ms to 4 ms.

## Review

Every quarter, and after every Firecracker, kernel or runner upgrade: run
`audit.sh`, and the [security audit](security-audit.md) checks.
