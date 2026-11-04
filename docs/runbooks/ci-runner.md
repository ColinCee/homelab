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
`sudo`, with Node 24 LTS, uv, Python 3.15, a JDK 25 (Temurin, at
`JAVA_HOME=/opt/jdk`), mise, bun, pnpm and the docker CLI with its compose
plugin (no daemon, so `docker compose config` works but containers do not)
already installed (pinned in `versions.env`) and on the job's `PATH`, so a job
needs no setup actions. mise installs whatever else a repo's `mise.toml` lists. The libraries Playwright's
Chromium needs are installed too, so a job runs `playwright install chromium`
and needs no `install-deps` step. The actions the CI repos use
(`CACHED_ACTIONS` in `versions.env`) are unpacked in the image, so "Set up job"
does not download them; a tag that has moved on is downloaded as usual until
the list is refreshed and the image rebuilt.

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
   artifact `homelab-fc-cache`. The job's last step does both with the shared
   action, which does nothing on any other event:

   ```yaml
   - uses: ColinCee/homelab/.github/actions/fc-cache@main
     with:
       paths: web/node_modules web/package-lock.json # optional workspace paths
   ```

   It tars every readable top-level folder of `/cache` (not `lost+found`) and
   the given paths, and fails the job rather than upload an archive bigger than
   the supervisor takes.
2. The supervisor checks for a newer one every minute, takes it only if its run
   was a successful push to the default branch, unpacks it (plain files,
   folders and links only, nothing outside the folder) and builds
   `/var/lib/homelab-ci/caches/<owner>__<repo>.ext4`, root-owned and read-only
   to others. VMs started after that use it.

A job working on another served repo's code can ask for that repo's disk with
one extra label, `fc-cache-<owner>--<name>` in lowercase:

```yaml
    runs-on: [self-hosted, linux, x64, homelab-fc, fc-cache-octo--widgets]
```

Its VM registers with that label too, and a VM without it never counts as
covering the job. The VM gets the named repo's disk only if that repo is
served, private and has a disk; otherwise it gets no cache disk and the job
still runs. Labels cannot stop GitHub handing a job without the label to such
a VM when both are queued in the same repo; that job then starts with the named
repo's disk, and the supervisor boots another VM for the labelled job.

A job should not restore GitHub's caches as well. Without a cache disk yet,
`/cache` starts empty and the job downloads as usual. The default branch's job
also puts `node_modules` and its `package-lock.json` on the disk (the action's
`paths`), copied rather than linked so TypeScript's incremental build info
stays valid.

There are five slots unless `/etc/homelab-ci/slots` says otherwise (see
Operating). A job running alone gets 4 vCPUs and 4 GiB; a job
that starts while others run gets 2 vCPUs and 2 GiB. A job that needs more memory loses the greedy
process, not the VM.

A VM starts only when the host's available memory (`MemAvailable` in
`/proc/meminfo`), less the VM's size, still leaves 3 GiB for the home's own
services; `/etc/homelab-ci/memory-reserve-mib` changes that amount. A booted VM
touches far less than its size, so this counts what is really free, not the
sizes added up. A job without the memory stays queued and is tried again on the
next poll; the log says so once per job. A standby gives way first: none boots
while a job waits for memory, and one waiting for another repo is stopped to
make room.

Only Colin's private repositories the token can see are served (its repository
access is the list; `served_repos` in the supervisor), and only those nobody else can
write to: a repo with any other collaborator or a deploy key that can write is skipped
(checked every minute and before its first VM), so only Colin
and his own apps (Kaizen, Renovate) can start a job here. Repos that have had a VM are
polled every 2 s; the rest once a minute. Only while private: privacy is
checked on every poll, and once a repo is public no new VM starts for it and its
idle ones are stopped. Before making a repo public, move its workflows to
GitHub-hosted runners and take it off the token's repository access.

## Design

- **One VM per job, never reused.** The supervisor polls GitHub every 2
  seconds (unchanged answers are free) for queued jobs with the label and boots
  a VM for each, up to two at once. The VM's root filesystem is a private copy, deleted with everything the
  job wrote. However the job ends, the VM ends, within seconds. A VM left
  without a job (cancelled before it started, or taken by another runner) is
  stopped about 30 to 40 seconds after it boots.
- **One VM waits ready.** A standby VM is kept booted and registered for the
  repo of the last job, so the next job there starts in about a
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
  (`fc-slotN`), chrooted, with Firecracker's syscall filter, no capabilities
  and a memory cap. All VMs share one cgroup (`firecracker`) at CPU weight 20,
  below the services' 100 (`system.slice`): weight only counts between siblings.
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
| Checks | [`ci-runner/tests/`](../../ci-runner/tests/): `audit.sh`, `test-network.sh`, `test-tools.sh`, `test-load.sh` |

Each slot has its own tap device and a private address pair of its own.

## Operating

From a checkout of `main` on the host:

```bash
sudo ci-runner/install-host.sh   # Firecracker, slot users, network, supervisor files
sudo ci-runner/build-rootfs.sh   # VM image and kernel
sudo ci-runner/tests/test-tools.sh  # the image's tools run for a job
sudo ci-runner/tests/audit.sh    # must pass before the supervisor runs
sudo systemctl enable --now homelab-ci-supervisor.service
journalctl -u homelab-ci-supervisor -f
```

`/etc/homelab-ci/slots` holds how many VMs run at once (5 when the file is
missing or not a number). `install-host.sh` writes it, keeping its value unless
run as `sudo SLOTS=4 ci-runner/install-host.sh`, and makes that many slot users
and taps; the supervisor reads it when it starts, and `setup-net.sh` and
`test-load.sh` read it too (a `SLOTS` variable wins). Change it only through
`install-host.sh`, so the network always has a tap for every slot. With N slots
the VMs use up to 2 + 2N GiB on paper (12 GiB for 5). A job touches far less
than its size (about 1 GB, see [the measurements](../ci-runner-performance.md)),
and the memory check above holds a VM back before the services' reserve is
reached, so 5 fits the 16 GiB host while jobs mostly wait on the network. A host
that already has the file keeps its number: to move one from 3 to 5, follow
[Changing the number of slots](#changing-the-number-of-slots).

The GitHub token is a fine-grained token with Administration read and write (to
create one-job registrations) and Actions read. Its repository access is the list
of served repositories: the supervisor serves every private repository of Colin's
the token can see, checked every minute, and nothing else. Store it without it reaching the screen or shell history:

```bash
bash -c 'read -rsp "Token: " t; echo; printf "%s" "$t" | sudo tee "$TOKEN_FILE" >/dev/null; sudo chmod 600 "$TOKEN_FILE"'  # TOKEN_FILE: see the supervisor
```

To serve another repository (sign it up), add it to the token's repository access
in GitHub (Settings, Developer settings, Fine-grained tokens): within a minute its
jobs asking for `homelab-fc` get VMs. Nothing changes on the host.

After every poll, failed ones too, the supervisor replaces
`/var/lib/homelab-ci/status/machines.json` (folder 0755, file 0644, written to a
temporary file and renamed, so a reader never sees half of it). The Control room
mounts the folder read-only at `/machines`. It holds only counts and repo names:

```json
{"updated": 1793000000, "slots": 5, "in_use": 1, "standby": 1,
 "cache_disks": ["ColinCee/example"], "github_ok": true,
 "mem_available_mib": 9216, "held_for_memory": 0}
```

`in_use` counts VMs started for a job, `standby` the ones waiting ready,
`cache_disks` the repos with a disk in the caches folder, and `github_ok` is
false when the last poll failed or was rate limited. `mem_available_mib` is the
host's free memory when the file was written, and `held_for_memory` counts queued
jobs not started because that memory would have dipped into the services' reserve. A failed write is logged
once and tried again next poll; it never holds up the poll.

Beside it, `live.json` holds what each running auto-kaizen Builder or Reviewer is
doing, the line its VM prints on its console (format in auto-kaizen's
`docs/live-view.md`). One background reader per VM reads that console for the
VM's whole life, so the console never stalls, and keeps only lines in that exact
format, at most 4 KB each, from VMs booted for `ColinCee/auto-kaizen`; everything
else is dropped unlogged. A VM's entry goes when it ends:

```json
{"updated": 1793000000,
 "lines": [{"task": 105, "who": "builder", "at": 1792999990, "heard": 1792999998, "text": "runs the tests (uv run pytest -q)"}]}
```

Re-run `build-rootfs.sh` after a kernel upgrade, since the VMs boot the host's
kernel, and after a runner upgrade. For a Firecracker or runner upgrade,
Renovate opens the PR; put the new release hash in `versions.env` by hand. Then
re-run `install-host.sh`, `build-rootfs.sh` and the audit.

## Checks

- **`audit.sh`** is the gate. It checks host settings, what a slot user could
  read or write after escaping a VM (the token, Docker, Colin's home, the
  image), that the image holds no keys or tokens and its runner user owns uv's
  Python folder, how a live Firecracker is
  confined, that a VM is told only its task, and runs the network test.
- **`test-network.sh`** boots a VM that must reach GitHub, PyPI and npm, must
  time out on every private address and the public address, and must not get
  out with another address. It then connects to the VM from a container and
  from the host and checks that the drop rule counted it. Every check must pass.
- **`test-tools.sh`** boots a VM and runs each baked-in tool (Node, uv, Java,
  mise, bun, pnpm, `docker compose`) as the runner user with the job's `PATH`.
  Run it after `build-rootfs.sh`.
- **`test-load.sh`** boots a VM in every slot (the count in
  `/etc/homelab-ci/slots`) at the size a job gets while others run (2 vCPUs,
  2 GiB), each spinning every CPU and holding all but 128 MiB of its free
  memory, while timing Home Assistant and Grafana once a second. It prints each
  service's median and worst response with the second it came (counted from
  when the VMs start), each failed request as `code@second`, and the host's lowest
  `MemAvailable` during the run, then one `PASS` or `FAIL` line naming the slot
  count. It fails, exiting 1 with the reasons on that line, when a VM does not
  boot or finish its burn, any request fails, or a worst response is over
  `MAX_RESPONSE` seconds (1 by default). On 4 November 2026, before the VMs'
  CPU weight moved to their shared cgroup, Grafana's worst was 0.7 to 3.8 s at
  3 to 5 slots, with 5 failed requests in some runs.

## Changing the number of slots

With the supervisor stopped (`sudo systemctl stop homelab-ci-supervisor`) and
its VMs finished:

1. Write the new number: `sudo SLOTS=5 ci-runner/install-host.sh`, which also
   makes that many slot users and taps.
2. `sudo ci-runner/tests/test-load.sh` must end with `PASS 5 slots`.
3. `sudo ci-runner/tests/audit.sh` must pass.
4. `sudo systemctl restart homelab-ci-supervisor`; it reads the number when it starts.

If the load test fails, go back to the old number with
`sudo SLOTS=3 ci-runner/install-host.sh` (your old count), start the
supervisor, and try 4.

## Review

Every quarter, and after every Firecracker, kernel or runner upgrade: run
`audit.sh`, and the [security audit](security-audit.md) checks.
