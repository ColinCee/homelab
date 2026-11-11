# CI runner (Firecracker)

CI for Colin's private repositories, each job in its own throwaway Firecracker microVM on the Beelink. Public repositories use
GitHub-hosted runners; the deploy runner is separate ([Deploying services](deploying-services.md)). How fast it is:
[CI runner performance](../ci-runner-performance.md).

## Using it

A job asks for a VM by label. A private repository's workflow never names a GitHub-hosted runner, not even as a
fallback (those runs are billed and can be refused), and a pull-request job uses no repository secrets.

| `runs-on` | For | VM |
|---|---|---|
| `[self-hosted, linux, x64, homelab-fc]` | Builds and tests | 4 vCPU and 4 GiB alone, 2 and 2 GiB beside other jobs; the repo's cache disk |
| add `fc-cache-<owner>--<name>` (lowercase) to a build | A job working on another served repo's code | That repo's cache disk, if it is served, private and has one; else none |

A job that runs out of memory loses the greedy process, not the VM.
GitHub can still hand a job without an `fc-cache-` label to a VM booted with one when both are queued in the same
repo; that job then starts with the named repo's disk.

The VM is Ubuntu 26.04 with the GitHub runner and these already on the job's `PATH`, so a job needs no setup
actions: git, build tools, Node, uv, one Python as `python` and `python3`,
JDK 25 (`JAVA_HOME=/opt/jdk`), mise, bun, pnpm, `gh`, gitleaks, shellcheck, `file`, `bwrap`, socat, passwordless
`sudo`, and Docker with buildx and compose (the engine starts on a job's first `docker` call; no sudo needed).
Versions are pinned in [`versions.env`](../../ci-runner/versions.env). mise installs whatever else a repo's
`mise.toml` lists. Playwright's Chromium libraries are installed, so `playwright install chromium` needs no
`install-deps`. The actions the CI repos use (`CACHED_ACTIONS`) are unpacked in the image; a tag that has moved on
downloads as usual until the list is refreshed.

The fastest job shape, as cortex's `.github/workflows/linux.yml`: checkout, one step that runs
`uv sync --locked --no-install-project` and copies `node_modules` from the cache disk when `package-lock.json`
matches the one beside it (else `npm ci`), then the tests.

### Cache disk

Each build VM gets its repo's cache disk at `/cache`: uv's and npm's caches and the Python environment from the
repo's last green push to its default branch. `UV_CACHE_DIR` (`/cache/uv`), `npm_config_cache` (`/cache/npm`) and
`UV_PROJECT_ENVIRONMENT` (`/cache/venv`) point at it. Each VM writes to its own snapshot, so nothing a job writes
outlives the VM and a branch never changes what the next job starts from. Do not restore GitHub's caches as well;
without a disk yet, `/cache` starts empty and the job downloads as usual.

To save it, make this the job's last step; it does nothing except on a push to the default branch, and never fails
the job (a cache too big or a missing path is a warning):

```yaml
- uses: ColinCee/homelab/.github/actions/fc-cache@main
  with:
    paths: web/node_modules web/package-lock.json # optional workspace paths
```

`node_modules` is copied, not linked, so TypeScript's incremental build info stays valid. The action tars `/cache`
onto the VM's outbox disk; once the VM ends, the supervisor takes it only from a passing push job on the default
branch newer than the disk's, and builds `/var/lib/homelab-ci/caches/<owner>__<repo>.ext4` without running any of
the repo's code (`unpack_cache` and `build_cache_image` in [`ci_supervisor.py`](../../scripts/ci_supervisor.py)). Nothing goes through GitHub
artifacts, whose storage quota, once full, fails every upload.

### What runs where

Only Colin's private repositories that the token can see and that nobody else can write to are served
(`served_repos`): a repo with another collaborator or a writable deploy key is skipped. Once a repo is public no
new VM starts for it and its idle ones stop. Before making one public, move its workflows to GitHub-hosted runners
and take it off the token's repository access.

## Operating

### First install

From a checkout of `main` on the host:

```bash
sudo ci-runner/install-host.sh   # Firecracker, slot users, network, supervisor files
sudo ci-runner/build-rootfs.sh   # VM image and kernel
sudo ci-runner/tests/test-tools.sh  # the image's tools run for a job; supervisor stopped first
sudo ci-runner/tests/audit.sh    # must pass before the supervisor runs
sudo systemctl enable --now homelab-ci-supervisor.service
journalctl -u homelab-ci-supervisor -f   # and each VM's own lines: journalctl -t homelab-ci-vm -f
```

`install-host.sh` also trims the SSD hourly, since the VMs' throwaway disks churn about 0.5 TB a day and untrimmed
they slow every write, and on the Beelink installs `homelab-ssd-wear.timer`, which writes the SSD's wear to
`ssd.prom` for OpenObserve every 15 minutes (`systemctl status homelab-ssd-wear.service`).

The GitHub token is fine-grained, with Administration read and write (for one-job registrations) and Actions
read. Store it without it reaching the screen or shell history:

```bash
bash -c 'read -rsp "Token: " t; echo; printf "%s" "$t" | sudo tee "$TOKEN_FILE" >/dev/null; sudo chmod 600 "$TOKEN_FILE"'  # TOKEN_FILE: see the supervisor
```

To serve another repository, add it to the token's repository access (GitHub Settings, Developer settings,
Fine-grained tokens): within a minute its `homelab-fc` jobs get VMs. Nothing changes on the host.

### Changing the Beelink while jobs run

A change goes in by merging it. Within a minute or two the Beelink takes it (`homelab-ci-update.timer` runs
`update.sh --follow`, which reads main's commit). A code change is live in about 2 minutes; one that rebuilds the
image, in about 5. `ci-runner/update-beelink.sh`, from Colin's PC, runs that pass now and shows what it did. `update.sh` refuses a commit not on main or older than the installed one, names the pull requests
each install ships, and writes `/etc/homelab-ci/commit` last, so that file says how
current the Beelink is.

- **Running jobs carry on.** Each VM is its own systemd unit (`homelab-ci-vm-<runner name>`), and a restarted
  supervisor takes the running VMs back in seconds.
- **Without a restart:** `run-vm.sh`, `vm-disks.sh`, `stop-vms.sh`, Firecracker and a rebuilt image go in while
  jobs run; new VMs get them.
- **With a restart:** the supervisor, network rules, systemd units or slot counts. The rules reload with the home's
  public address in one transaction, so running VMs never see it unblocked.
- **A change whose install or image fails twice** is tried again an hour after its last failure. A second `install-host.sh` waits
  for the first and names it.
- **Going back** is a hand step, which pins the Beelink there until
  `sudo rm /etc/homelab-ci/pinned`: `sudo ROLLBACK=1 /usr/local/lib/homelab-ci/update.sh <commit>`.
- **Kernel upgrade:** the VMs boot the Beelink's kernel, so rebuild by hand (`sudo ci-runner/build-rootfs.sh`).
- **Version upgrade:** a stale hash fails CI (`scripts/versions_hash.py`), which prints the right one; a passing
  non-major bump merges itself. Run the audit.
- **Stopping every VM** on purpose: `sudo ci-runner/stop-vms.sh`.
- **First time on a new host**, `update.sh` is not there yet: run `sudo ci-runner/install-host.sh` once from a checkout
  of main; the first update then rebuilds the image (several minutes).
- **Run any wait on the Beelink as a named systemd unit**, never a loop on your own connection.

### Changing the number of slots

| File | Default | Sets |
|---|---|---|
| `/etc/homelab-ci/slots` | 5 | Build VMs at once |
| `/etc/homelab-ci/standbys` | 2 (0 for none) | VMs kept booted and waiting; a standby always has 4 vCPUs |
| `/etc/homelab-ci/memory-reserve-mib` | 3072 | Memory left for the home's services |

Change the slot count only through `install-host.sh`, so the network has a tap for every slot:

1. `sudo SLOTS=5 ci-runner/install-host.sh`. It makes the slot users and taps and restarts the supervisor.
2. `sudo ci-runner/tests/audit.sh` must pass.

The Beelink then load-tests the new count itself: `update.sh` starts `homelab-ci-load-test.service` when
`/var/lib/homelab-ci/load-test.json` has no result for this count and this `test-load.sh`. The unit drains the host
(no new VMs, standbys stopped, up to 65 minutes for running VMs to end), runs `test-load.sh`, and a `FAIL` lowers
`/etc/homelab-ci/slots` by one, never below 3, which the next pass tests. Follow it with
`journalctl -u homelab-ci-load-test`.

With N slots the VMs use up to 2 + 2N GiB on paper, more than the host has. That works because a job touches far
less than its size (about 1 GB) and a VM starts only when the host's `MemAvailable`, less the VM's size, still
leaves the reserve, so jobs get memory in start order.

### Status files

The supervisor writes these for OpenObserve, `server-health.sh` and speed reports. What each field counts is in the
function named.

| File | Holds | Code |
|---|---|---|
| `/var/lib/homelab-ci/status/machines.json` (0644, replaced after every poll) | Slots, VMs busy and waiting ready, cache disks, free memory, jobs queued and held, the last load test. Counts and repo names only | `status` |
| `status/waits.jsonl` (0644, 30 days) | One line per job that waited, when it stops: seconds per kind of wait and the reason for the longest | `wait_kind` |
| `/var/lib/homelab-metrics/ci-waits.prom` | The longest-waiting job's seconds per kind, each workflow's longest queued job, busy VMs, failed polls | `homelab_ci_*` |
| `/var/lib/homelab-metrics/ci-vm-memory.prom` | VMs ended, near full (over 80%) and out-of-memory kills | `MEMORY_METRICS` |
| `/etc/homelab-ci/waits.json` in each VM | How long its repo and label's queued jobs had waited and why | `vm_waits` |

`machines.json` looks like this; `held_for_memory` counts jobs held for memory or a busy disk, `queued` every
queued job not started yet, and `github_ok` is false when the last poll failed or was rate limited:

```json
{"updated": 1793000000, "slots": 5, "in_use": 1, "standby": 1,
 "cache_disks": ["ColinCee/example"], "github_ok": true,
 "mem_available_mib": 9216, "held_for_memory": 0, "queued": 0, "in_use_repos": {"ColinCee/example": 1}, "draining": false,
 "load_test": {"slots": 5, "passed": true, "ran": true, "line": "PASS 5 slots: ...", "at": 1793000000, "commit": "...", "test": "..."}}
```

The console reader keeps only lines in the exact memory format; everything else on the console is dropped unlogged.

When a VM ends, the journal says how much host memory it touched, what it wrote to the SSD, and what its job needed
with its out-of-memory kills: `slot N: VM ended (exit 0), its memory peaked at X MiB of Y, it wrote Z MiB` and
`slot N: <runner> (build) needed at least X MiB of Y, K out-of-memory kills`. Its own lines are in
`journalctl -t homelab-ci-vm`.

### Why a job waits

The supervisor's journal logs each queued job it has not started, when it first waits and every 3 minutes after:

```bash
journalctl -u homelab-ci-supervisor --since -1h | grep -E 'has waited|stopped waiting'
```

| Line ends with | Means | Do |
|---|---|---|
| `all N build slots in use` | Every slot holds a VM | More slots, or shorter jobs |
| `memory (X MiB free above the R MiB reserve, a VM needs Y)` | Starting it would eat into the services' reserve | Fewer or smaller VMs, not more |
| `the disk (tasks waited on it P% of the last minute)` | Disk wait over 50%, or over 25% (then one VM a minute) | As memory |
| `a VM booting for it` | Logged after 3 minutes only | Slow boots |
| `a VM ready for it that GitHub has not given the job` | Its runner is online and idle; logged after 3 minutes only | GitHub is slow |
| `no VM has its labels (...)` | It never starts here | Fix its `runs-on` |

`(GitHub had queued it Xm before)` marks a job the supervisor first saw 90 s or more after GitHub queued it: it sat
waiting or pending on GitHub, the supervisor polled the repo late (`slow poll:` lines) or restarted. A logged job
gets `stopped waiting after Xm, Ym since GitHub queued it` once it starts or is cancelled.

## Design

- **One VM per job, never reused.** The supervisor polls GitHub every 2 seconds for queued jobs with its labels
  (repos that have never had a job here once a minute) and boots a VM for each. A VM's disks are device-mapper
  snapshots of the images ([`vm-disks.sh`](../../ci-runner/vm-disks.sh)), so it starts in seconds and its writes are
  thrown away with it. However the job ends, the VM ends within seconds; one left without a job is stopped about
  30 to 40 seconds after boot.
- **VMs wait ready.** Standbys are kept booted and registered for the repos and labels likely next, so a job starts
  in about a second instead of about 11. Standbys follow the last job, then each running one (a run's next job
  finds one booted while the one before it ran); each costs about 450 MB idle, is replaced every 45 minutes, and
  gives way to waiting jobs. Which boots when is `standby_wants` in the supervisor.
- **Oldest job first, with one exception.** A started run's later jobs go before a new run's first job, for at
  most 10 minutes (`start_order`). GitHub, not the supervisor, picks which of a repo's queued jobs a VM takes.
- **The home's services come first.** A VM starts only while it leaves the memory reserve and while tasks waited on
  the disk under half the last minute (one a minute over a quarter; `DISK_WAIT_LIMIT`). A VM never swaps: its memory
  cap also counts the host's cache of its disk ([`run-vm.sh`](../../ci-runner/run-vm.sh)). In the VM, `ci-trim-cache`
  drops file cache past a quarter of its memory and the balloon returns freed pages, so the reserve check sees what
  is really free. All VMs share one cgroup at CPU weight 20, below the services' 100.
- **Just-in-time registration.** Each VM gets a runner registration good for exactly one job. The GitHub token never
  leaves the host.
- **Jobs have the internet, not the home network.** VMs reach the internet through NAT. The host, the LAN, the
  tailnet, Docker networks, other slots and the home's public address (the router answers on it from inside) are
  dropped. A VM may use only its own address, and nothing, the host included, can open a connection to a VM. The
  iptables accept is limited to the uplink, so it opens nothing private even without the nftables table.
- **Firecracker is jailed:** as the slot's own user (`fc-slotN`), chrooted, with its syscall filter, no
  capabilities and a memory cap. A lock stops two runs sharing a slot; a dead run's leftovers are killed by the next.
- **Installed from main only.** Everything lives root-owned under `/usr/local/lib/homelab-ci`, `/var/lib/homelab-ci`
  and `/etc/homelab-ci`. Only `update.sh` changes it, from a commit on `main`; stack deploys never touch it.

| Piece | Where |
|-------|-------|
| Versions, release hashes | [`ci-runner/versions.env`](../../ci-runner/versions.env) |
| Host setup | [`ci-runner/install-host.sh`](../../ci-runner/install-host.sh) |
| Network rules | [`ci-runner/net/`](../../ci-runner/net/), table `inet homelab_ci`, `homelab-ci-net.service` |
| VM image | [`ci-runner/rootfs/`](../../ci-runner/rootfs/), built by [`build-rootfs.sh`](../../ci-runner/build-rootfs.sh) |
| Boot one VM | [`ci-runner/run-vm.sh`](../../ci-runner/run-vm.sh) |
| Supervisor | [`scripts/ci_supervisor.py`](../../scripts/ci_supervisor.py), `homelab-ci-supervisor.service` |
| Updates | [`ci-runner/update.sh`](../../ci-runner/update.sh), `homelab-ci-update.timer` |

## Checks

| Script in [`ci-runner/tests/`](../../ci-runner/tests/) | Checks |
|---|---|
| `audit.sh` | The gate: host settings, what a slot user could read or write after escaping a VM (the token, Docker, Colin's home, the image), that the image holds no keys, how a live Firecracker is confined, that a VM is told only its task, and the network test |
| `test-network.sh` | A VM reaches GitHub, PyPI and npm, times out on every private address and the public one, cannot get out with another address, and cannot be reached from a container or the host |
| `test-tools.sh` | Each baked-in tool runs as the runner user, and Docker builds, runs with a published port and serves a page. Run after `build-rootfs.sh` with the supervisor stopped (`sudo systemctl stop homelab-ci-supervisor`) and its VMs finished, since they outlive it (`sudo ci-runner/stop-vms.sh`): it boots in slot 0 |
| `test-load.sh` | A VM in every slot at 2 vCPU and 2 GiB, flat out on CPU and memory, while timing Home Assistant and OpenObserve once a second; fails on a failed request or a worst response over `MAX_RESPONSE` (1 s) |

Every quarter, and after every Firecracker, kernel or runner upgrade: run `audit.sh` and the
[security audit](security-audit.md) checks.
