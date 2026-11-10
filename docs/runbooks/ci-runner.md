# CI runner (Firecracker)

CI for Colin's private repositories, each job in its own throwaway Firecracker microVM on the Beelink, and Auto
Kaizen's builds also on Colin's PC when it gives slots ([PC build slots](#pc-build-slots)). Public repositories use
GitHub-hosted runners; the deploy runner is separate ([Deploying services](deploying-services.md)). How fast it is:
[CI runner performance](../ci-runner-performance.md).

## Using it

A job asks for a VM by label. A private repository's workflow never names a GitHub-hosted runner, not even as a
fallback (those runs are billed and can be refused), and a pull-request job uses no repository secrets.

| `runs-on` | For | VM |
|---|---|---|
| `[self-hosted, linux, x64, homelab-fc]` | Builds and tests | 4 vCPU and 4 GiB alone, 2 and 2 GiB beside other jobs; the repo's cache disk |
| `[..., homelab-fc-light]` | Auto Kaizen's merge and agent steps | 1 vCPU, 2 GiB, no cache disk, 3-hour timeout |
| `[..., homelab-fc-small]` | A light job that runs no agent (peaks under 700 MiB) | 1 vCPU, 1 GiB |
| `[..., homelab-fc-deploy]` | A merge's deploy | 1 vCPU, 1 GiB, a standby no other job can take |
| add `fc-cache-<owner>--<name>` (lowercase) to a build | A job working on another served repo's code | That repo's cache disk, if it is served, private and has one; else none |

Light, small and deploy jobs run in light slots of their own, so a merge waiting on CI never takes the slot CI
needs, and a light VM never takes a build job. The light standby also takes small jobs, which then run at 2 GiB. A job that runs out of memory loses the greedy process, not the VM.
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

### Handoffs

Files a job hands to later jobs, in place of GitHub artifacts:

```yaml
- uses: ColinCee/homelab/.github/actions/handoff@main
  with:
    put: builder          # a-z, 0-9, -
    path: out/            # a file or folder
    readers: review land  # jobs of this run that may get it
    later: line           # jobs of any run that may get it
    days: 90              # at most 90
- uses: ColinCee/homelab/.github/actions/handoff@main
  with:
    get: builder
    from: build           # the job that put it
    run: ${{ needs.line.outputs.held_run }}  # default this run
    path: ${{ runner.temp }}/in
```

- `put` never fails the job; `get` fails if the item is not there, or not for this job. `get` waits up to `wait`
  seconds (at most 1800) and answers `none` once the putting job has finished and its outbox is filed.
- Items are filed under `/var/lib/homelab-ci/handoff/<owner>__<repo>/<run>/<job>/` with the job name GitHub shows
  for that VM's runner, so no job can put under another's name. Root only (`0700`).
- `later` readers count only for a default-branch run that is not a pull request's. Past 20 GiB a repo's new items
  are dropped.
- A job puts handoffs or a cache, not both, and only a build VM's cache is taken.
- Job names need no spaces: a matrix or called workflow's job (`build (3.12)`, `call / build`) cannot put or be
  named in `from`.
- A VM gets at most 50 answers (handoffs and `ci-capacity` together) and still cannot open any connection to the
  Beelink: asks go out on its console, answers come back on a read-only inbox disk.

### Free build machines

`ci-capacity` in any job VM prints the build machines' counts as JSON, no repo names, so Auto Kaizen's task start
can see how many are free:

```json
{"updated": 1793000000, "slots": 5, "in_use": 1, "standby": 1, "queued": 0, "held_for_memory": 0,
 "in_use_other_repos": 1, "pc_up": 1, "pc_slots": 6, "pc_in_use": 2}
```

Fields are as in [`machines.json`](#status-files); `in_use_other_repos` counts build VMs running other repos' jobs.
It fails within 15 seconds (`CAPACITY_WAIT`) when nothing answers. Auto Kaizen runs no more tasks at once than the
build slots (5, set in its `build.yml` and `watch.yml`): change them together.

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

A change goes in by merging it. Within a minute or two each host takes it (`homelab-ci-update.timer` runs
`update.sh --follow`): the Beelink reads main's commit, and the PC reads the commit and job image the Beelink runs,
so it never takes a commit before its image is built. A code change is live on both in about 2 minutes; one that
rebuilds the image, in about 5. `ci-runner/update-hosts.sh`, from Colin's PC, runs that pass on both now and shows
what it did. `update.sh` refuses a commit not on main or older than the installed one, names the pull requests
each install ships (the Control room links them), and writes `/etc/homelab-ci/commit` last, so that file says how
current a host is.

- **Running jobs carry on.** Each VM is its own systemd unit (`homelab-ci-vm-<runner name>`), and a restarted
  supervisor takes the running VMs back in seconds.
- **Without a restart:** `run-vm.sh`, `vm-disks.sh`, `stop-vms.sh`, Firecracker and a rebuilt image go in while
  jobs run; new VMs get them.
- **With a restart:** the supervisor, network rules, systemd units or slot counts. The rules reload with the home's
  public address in one transaction, so running VMs never see it unblocked.
- **A change whose install or image fails twice** is tried again an hour after its last failure. A second `install-host.sh` waits
  for the first and names it.
- **Going back** is a hand step on each host, Beelink first, which pins the host there until
  `sudo rm /etc/homelab-ci/pinned`: `sudo ROLLBACK=1 /usr/local/lib/homelab-ci/update.sh <commit>`.
- **Kernel upgrade:** the VMs boot the Beelink's kernel, so rebuild by hand (`sudo ci-runner/build-rootfs.sh`, then
  delete `/var/lib/homelab-ci/images/commit` so the PC takes it).
- **Version upgrade:** a stale hash fails CI (`scripts/versions_hash.py`), which prints the right one; a passing
  non-major bump merges itself. Run the audit.
- **Stopping every VM** on purpose: `sudo ci-runner/stop-vms.sh`.
- **First time on a host**, `update.sh` is not there yet: run `sudo ci-runner/install-host.sh` once from a checkout
  of main; the first update then rebuilds the image (several minutes).
- **Run any wait on the Beelink as a named systemd unit**, never a loop on your own connection.

### Changing the number of slots

| File | Default | Sets |
|---|---|---|
| `/etc/homelab-ci/slots` | 5 | Build VMs at once |
| `/etc/homelab-ci/light-slots` | 5 (none while the file is missing); on the Beelink at least `LIGHT_SLOTS_MIN` in [`slots.sh`](../../ci-runner/slots.sh), never lowered; 0 on the PC | Light VMs at once |
| `/etc/homelab-ci/standbys`, `light-standbys`, `deploy-standbys`, `small-standbys` | 2, 3, 1, 1 (0 for none) | VMs kept booted and waiting; a build standby always has 4 vCPUs |
| `/etc/homelab-ci/memory-reserve-mib` | 3072 | Memory left for the home's services |

Change slot counts only through `install-host.sh`, so the network has a tap for every slot:

1. `sudo SLOTS=5 ci-runner/install-host.sh` (or `LIGHT_SLOTS=`; raising `LIGHT_SLOTS_MIN` and merging raises the
   Beelink's). It makes the slot users and taps and restarts the supervisor.
2. `sudo ci-runner/tests/audit.sh` must pass.

The Beelink then load-tests the new count itself: `update.sh` starts `homelab-ci-load-test.service` when
`/var/lib/homelab-ci/load-test.json` has no result for this count and this `test-load.sh`. The unit drains the host
(no new VMs, standbys stopped, up to 65 minutes for running VMs to end), runs `test-load.sh`, and a `FAIL` lowers
`/etc/homelab-ci/slots` by one, never below 3, which the next pass tests. Follow it with
`journalctl -u homelab-ci-load-test`.

With N build slots the VMs use up to 2 + 2N GiB on paper, plus 1 to 2 GiB per light slot, more than the host has.
That works because a job touches far less than its size (about 1 GB) and a VM starts only when the host's
`MemAvailable`, less the VM's size, still leaves the reserve, so builds get memory in start order and light VMs
mostly wait.

### Status files

The supervisor writes these for the Control room, OpenObserve and speed reports. What each field counts is in the
function named.

| File | Holds | Code |
|---|---|---|
| `/var/lib/homelab-ci/status/machines.json` (0644, replaced after every poll) | Slots, VMs busy and waiting ready, cache disks, free memory, jobs queued and held, the PC's slots, the last load test. Counts and repo names only | `status` |
| `status/waits.jsonl` (0644, 30 days) | One line per job that waited, when it stops: seconds per kind of wait and the reason for the longest | `wait_kind` |
| `status/live.json` | Each running auto-kaizen Builder's and Reviewer's latest console line (auto-kaizen's `docs/live-view.md`) | `LIVE_LINE` |
| `status/sessions/<runner name>.jsonl` (root only, 30 MB cap, 30 days) | Auto-kaizen agents' whole sessions for the Control room's chat page (auto-kaizen's `docs/live-session.md`) | `SESSION_LINE` |
| `/var/lib/homelab-metrics/ci-waits.prom` | The longest-waiting job's seconds per kind, each workflow's longest queued job, busy VMs, failed polls | `homelab_ci_*` |
| `/var/lib/homelab-metrics/ci-vm-memory.prom` | VMs ended, near full (over 80%) and out-of-memory kills by kind of job | `MEMORY_METRICS` |
| `/etc/homelab-ci/waits.json` in each VM | How long its repo and label's queued jobs had waited and why | `vm_waits` |

`machines.json` looks like this; `held_for_memory` counts jobs held for memory or a busy disk, `queued` every
queued job not started yet, and `github_ok` is false when the last poll failed or was rate limited:

```json
{"updated": 1793000000, "slots": 5, "in_use": 1, "standby": 1,
 "light_slots": 5, "light_in_use": 2, "light_standby": 1, "deploy_standby": 1, "small_standby": 1, "cache_disks": ["ColinCee/example"], "github_ok": true,
 "mem_available_mib": 9216, "held_for_memory": 0, "queued": 0, "in_use_repos": {"ColinCee/example": 1}, "draining": false,
 "pc": {"up": true, "slots": 6, "in_use": 2},
 "load_test": {"slots": 5, "passed": true, "ran": true, "line": "PASS 5 slots: ...", "at": 1793000000, "commit": "...", "test": "..."}}
```

The console reader keeps only lines in the exact live and session formats, from VMs booted for
`ColinCee/auto-kaizen`, and files a session by the VM it came from, never by the line, so one VM cannot write into
another's. Everything else on the console is dropped unlogged.

When a VM ends, the journal says how much host memory it touched, what it wrote to the SSD, and what its job needed
with its out-of-memory kills: `slot N: VM ended (exit 0), its memory peaked at X MiB of Y, it wrote Z MiB` and
`slot N: <runner> (<kind>) needed at least X MiB of Y, K out-of-memory kills`. Its own lines are in
`journalctl -t homelab-ci-vm`.

### Why a job waits

The supervisor's journal logs each queued job it has not started, when it first waits and every 3 minutes after:

```bash
journalctl -u homelab-ci-supervisor --since -1h | grep -E 'has waited|stopped waiting'
```

| Line ends with | Means | Do |
|---|---|---|
| `all N build slots in use` (or `light`) | Every slot of its kind holds a VM; a light job adds `, one more kept for deploys` or `for small jobs` per slot kept for a standby not yet up | More slots, or shorter jobs |
| `memory (X MiB free above the R MiB reserve, a VM needs Y)` | Starting it would eat into the services' reserve | Fewer or smaller VMs, not more |
| `an older light job waiting for memory` | Queued behind a 2 GiB light job held for memory | As memory |
| `the disk (tasks waited on it P% of the last minute)` | Disk wait over 50%, or over 25% (then one VM a minute) | As memory |
| `a VM booting for it` | Logged after 3 minutes only | Slow boots |
| `a VM ready for it that GitHub has not given the job` | Its runner is online and idle; logged after 3 minutes only | GitHub is slow |
| `no VM has its labels (...)` | It never starts here | Fix its `runs-on` |

`(GitHub had queued it Xm before)` marks a job the supervisor first saw 90 s or more after GitHub queued it: it sat
waiting or pending on GitHub, the supervisor polled the repo late (`slow poll:` lines) or restarted. A logged job
gets `stopped waiting after Xm, Ym since GitHub queued it` once it starts or is cancelled.

The "stuck waiting for a test VM" alert gives the longest-waiting job's minutes per kind of wait, and for a missing
label its repo and labels. Its own number is the Control room's, counted from when GitHub created the task's next
job; minutes beyond the kinds added up mean the job is still waiting or pending on GitHub (a concurrency group),
which no runner can take, so look at the task's run on GitHub. Raise its 20 minutes only when the waits are
expected, such as a burst of tasks on full slots. The "Kaizen's Watcher stopped running" alert says how long the
Watcher's job and Auto Kaizen's longest job have waited for a VM: a waiting job means a capacity problem, none means
GitHub is not starting jobs. No VM starts while every poll fails, so "CI supervisor's poll keeps failing" fires
after 10 failures in a row for 2 minutes.

## PC build slots

Colin's gaming PC gives Auto Kaizen extra build slots from a Hyper-V Ubuntu VM, `kaizen-ci` (VHDX on D:, nested
virtualisation, its own NAT switch 192.168.250.0/24, no shared folders or guest file copy). It runs the same
Firecracker VMs and network rules (`install-host.sh` with `ROLE=pc`) and keeps one idle registered runner per slot it
gives, so GitHub hands a `homelab-fc` build to whichever it picks. Light jobs stay on the Beelink.

- **No GitHub key in the guest.** It reaches the Beelink only as `ci-peer` over SSH on port 2222 of the Beelink's
  Tailscale address (Tailscale SSH answers port 22 and would skip the forced command), forced into `socat` to the
  supervisor's socket. Through it the Beelink registers the PC's `pc-<slot>-<time>` runners (at most
  `/etc/homelab-ci/peer-slots-max`), reads their VMs' live lines, sessions and handoff asks with the same checks as a
  local VM's, files their outboxes and sends cache disks; the PC also asks which commit and image to run. A PC
  runner still offline 10 minutes after registering, or idle 45 minutes, is deleted (`peer_*` in the supervisor).
- **Image:** copied from the Beelink compressed (about 1.2 GB, about 2 minutes at the 10 MB/s measured between
  them, at most 4 an hour) and used only if it hashes to the Beelink's `images/sha256`. Until it lands, new PC VMs
  boot the old image.
- **Capacity:** on Windows, `D:\kaizen-ci\kaizen-pc.ps1 6` (slots), `off` (finish running jobs, then power the guest
  off and give its RAM back) or `on` (the last number). The guest holds 4 vCPU and 4 GiB per VM; its own size is
  set once in Hyper-V for the most slots it will give.
- **Gaming:** `keeper.ps1`, run by `keeper-tray.pyw` from Colin's Startup folder (a tray dot: green busy, amber
  paused, grey off, red restarting; its menu sets the slots or quits), gives 0 slots while a full-screen app has the
  screen for 15 s or another `gpu` lease is held, caps the guest at 10% of the CPU, and gives the slots back after 5
  quiet minutes. The tray app judges full screen (Windows' flag, or the front window covering its monitor, so a
  full-screen video pauses jobs too) and must be running, or nothing pauses. GPU load is no signal: the desktop alone
  sits near 30%. Ready runners stop at once; a job still running after 20 minutes is stopped and the Watcher reruns
  it as machine trouble. `lease free --force --name kaizen-ci` powers the guest off at once. Its lease's status, after
  `kaizen-ci` in the band above Claude's prompt, is the PC runners busy/online (`‖` while paused).
- **Task cap:** the keeper sets auto-kaizen's `KAIZEN_TASK_SLOTS` (with Colin's `gh`) to 5 plus the PC VMs up (down
  at once, up after 10 minutes). If the PC dies with it raised, extra tasks wait for a machine;
  `gh variable set KAIZEN_TASK_SLOTS --body 5 -R ColinCee/auto-kaizen` sets it back.

Setting it up (the Hyper-V VM, its first admin step and Colin's sign-in are his; the rest an agent does):

1. The guest: Ubuntu 24.04 cloud image as a Gen2 VM with `Set-VMProcessor -ExposeVirtualizationExtensions $true`,
   static memory of 2 GiB plus 4.25 GiB per slot it will ever give, and Colin in Hyper-V Administrators (the
   keeper starts it and caps its CPU).
2. In the guest, from a checkout of `main`: `sudo apt install docker.io` (its network rules need Docker's chain),
   then `sudo ROLE=pc SLOTS=<most slots> REASON="homelab#<n>: <why>" ci-runner/install-host.sh`; the job image
   comes from the Beelink with the first update. Then `ssh-keygen -t ed25519 -N "" -f /etc/homelab-ci/peer-key`, the
   door's address in `/etc/homelab-ci/peer` (`ssh://ci-peer@<the Beelink's Tailscale address>:2222`) and that port's
   host key in root's `known_hosts`.
3. On the Beelink: the guest's `peer-key.pub` as `/etc/homelab-ci/peer.pub`, the PC's Tailscale address (what the
   guest's traffic leaves Windows as) in `/etc/homelab-ci/peer-from`, then
   `sudo PEER_SLOTS=<most slots> REASON=... ci-runner/install-host.sh` (it opens the door: user, keys, sshd).
4. On Windows: the `lease` command from ColinCee/agents (`npm run install-lease`, a version with `--status`),
   `ci-runner/pc/keeper.ps1`, `kaizen-pc.ps1` and `keeper-tray.pyw` copied to `D:\kaizen-ci`, a Startup-folder
   `kaizen-ci-keeper.vbs` with `CreateObject("WScript.Shell").Run "uvw run --no-project --quiet --python 3.14
   --with pystray --with pillow D:\kaizen-ci\keeper-tray.pyw", 0`, `gh` signed in as Colin, then
   `kaizen-pc.ps1 <slots>`. Check `pc-*` runners show online in auto-kaizen's runner list before trusting the cap.

Switching it off for good: `kaizen-pc.ps1 off`, wait for the guest to power off, choose Quit on the tray dot,
remove `kaizen-ci-keeper.vbs` from the Startup folder, `sudo PEER_SLOTS=0 ci-runner/install-host.sh` on the Beelink
(closes the door), and set the task cap to 5.

## Design

- **One VM per job, never reused.** The supervisor polls GitHub every 2 seconds for queued jobs with its labels
  (repos that have never had a job here once a minute) and boots a VM for each. A VM's disks are device-mapper
  snapshots of the images ([`vm-disks.sh`](../../ci-runner/vm-disks.sh)), so it starts in seconds and its writes are
  thrown away with it. However the job ends, the VM ends within seconds; one left without a job is stopped about
  30 to 40 seconds after boot.
- **VMs wait ready.** Standbys are kept booted and registered for the repos and labels likely next, so a job starts
  in about a second instead of about 11. Build standbys follow the last build job, then each running one (a task's
  Reviewer finds one booted while its Builder ran); each costs about 450 MB idle and is replaced every 45 minutes.
  Standbys give way to waiting jobs, except the deploy and small ones, which a merge's deploy and a task's first step
  need. Which boots when is `standby_wants` and the light, deploy and small counterparts in the supervisor.
- **Oldest job first, with two exceptions.** A started task run's later jobs (its review, merge) go before a new
  run's first build or other workflows' light jobs, for at most 10 or 5 minutes (`start_order`). GitHub, not the
  supervisor, picks which of a repo's queued jobs a VM takes.
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
| `test-handoff-image.sh` | Handoffs and `ci-capacity` work in the image on directly read loop devices, as in a VM; CI runs it when a pull request changes the image or handoffs |
| `test-load.sh` | A VM in every slot at 2 vCPU and 2 GiB, flat out on CPU and memory, while timing Home Assistant and OpenObserve once a second; fails on a failed request or a worst response over `MAX_RESPONSE` (1 s) |

Every quarter, and after every Firecracker, kernel or runner upgrade: run `audit.sh` and the
[security audit](security-audit.md) checks.
