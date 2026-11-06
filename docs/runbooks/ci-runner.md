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

A private repository's workflow never names a GitHub-hosted runner, not even as a
fallback, since those runs are billed and can be refused; a pull-request job uses
no repository secrets.

The VM is Ubuntu 24.04 with the GitHub runner, git, build tools, the system
python3 (also as `python`), shellcheck, `file` and passwordless `sudo`, with Node 24 LTS, uv, Python 3.15, a JDK 25 (Temurin, at
`JAVA_HOME=/opt/jdk`), mise, bun, pnpm, the GitHub CLI (`gh`), gitleaks, bubblewrap
(`bwrap`), socat and Docker with its buildx and compose
plugins (the engine starts on a job's first `docker` call, so `docker build`,
`docker run -p` and `docker push` work as on GitHub's runner, without sudo)
already installed (pinned in `versions.env`) and on the job's `PATH`, so a job
needs no setup actions. mise installs whatever else a repo's `mise.toml` lists. The libraries Playwright's
Chromium needs are installed too, so a job runs `playwright install chromium`
and needs no `install-deps` step. The actions the CI repos use
(`CACHED_ACTIONS` in `versions.env`) are unpacked in the image, so "Set up job"
does not download them; a tag that has moved on is downloaded as usual until
the list is refreshed and the image rebuilt. A change to the image's tools or
actions reaches jobs only once `build-rootfs.sh` is re-run on the host.

The fastest shape for a job, as cortex's `.github/workflows/linux.yml` does it:
checkout, then one step that runs `uv sync --locked --no-install-project` and
copies `node_modules` from the cache disk when `package-lock.json` matches the
one beside it (falling back to `npm ci`), then the tests. Measured results are
in [CI runner performance](../ci-runner-performance.md).

### Cache disk

Each VM gets its repo's **cache disk** at `/cache`: uv's and npm's caches and
the Python environment from the repo's last green push to its default branch.
Each VM gets a snapshot of it (`ci-runner/vm-disks.sh`, like the root disk), so a
job can install into it but nothing it writes outlives the VM, and a branch can
never change what the next job starts from. The VM points `UV_CACHE_DIR` (`/cache/uv`),
`npm_config_cache` (`/cache/npm`) and `UV_PROJECT_ENVIRONMENT` (`/cache/venv`)
at it, so `uv sync` and `npm ci` find almost everything in place.

How it is made, without the host running any of the repo's code:

1. Every VM gets an empty **outbox**: a raw disk, after its other disks, kept on
   the host after the VM as `/var/lib/homelab-ci/outbox/<runner name>.tar`. A green
   push to the default branch tars `/cache` onto it with the shared action, the
   job's last step, which does nothing on any other event:

   ```yaml
   - uses: ColinCee/homelab/.github/actions/fc-cache@main
     with:
       paths: web/node_modules web/package-lock.json # optional workspace paths
   ```

   It tars every readable top-level folder of `/cache` (not `lost+found`, nor
   one a given path starts with: the disk holds the last copy of `web/` there)
   and the given paths, then writes a header naming its run. It never fails the
   job: a cache it cannot save (too big, a path missing) is a warning, and the
   next jobs start from the last one. Nothing goes through GitHub, whose artifact
   storage has a quota that, once full, fails every upload.
2. Once the VM has ended, the supervisor reads the header and asks GitHub about
   that run: it takes the tar only if the run was a push to the default branch and
   its job on this VM's runner passed (it waits up to an hour for GitHub to show the
   job finished), and only if the run is newer than the disk's. It unpacks it
   (plain files, folders and links only, nothing outside the folder; a file or link
   held twice takes its later entry) and builds
   `/var/lib/homelab-ci/caches/<owner>__<repo>.ext4`, root-owned and read-only
   to others. VMs started after that use it. Every outbox is deleted once judged.

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

### Handoffs

Files a job hands to later jobs, in place of GitHub artifacts (whose storage quota,
once full, fails every upload). `put` never fails the job; `get` fails if the item
is not there, or not for this job:

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

1. `put` writes every item the job has put so far onto the VM's outbox (as the
   cache does, header last). Once the VM has ended, the supervisor files each item
   under `/var/lib/homelab-ci/handoff/<owner>__<repo>/<run>/<job>/`, with the job
   name GitHub shows for that VM's runner, whatever the job's result: no job can put
   under another's name. It is deleted after its `days`; past 20 GiB a repo's new
   items are dropped. A job puts handoffs or a cache, not both, and only a build
   VM's cache is taken, never a light one's.
2. `get` prints an ask on the console. The VM's console reader asks GitHub which job
   runs on that VM, and answers on the VM's read-only **inbox** disk (the item after
   512 bytes, then a header): the item if that job is one of its `readers` (same run)
   or `later` readers, `none` once the job that would put it has finished and its
   outbox is filed, else it waits up to `wait` seconds. A VM still cannot open any
   connection to the Beelink. `wait` is at most 1800 seconds. `later` readers
   are honoured only for a run of the default branch that is not a pull request's,
   and a VM gets at most 50 answers.
3. Item names are `a-z`, `0-9` and `-`; job names need no spaces, so a matrix or
   called workflow's job (`build (3.12)`, `call / build`) cannot put or be named in
   `from`. Handoffs are root's alone on the Beelink (`0700`).

There are five slots unless `/etc/homelab-ci/slots` says otherwise (see
Operating). Auto Kaizen runs no more tasks at once than this number, set in its
`build.yml` and `watch.yml`; change them with it. A job running alone gets 4 vCPUs and 4 GiB; a job
that starts while others run gets 2 vCPUs and 2 GiB. A job that needs more memory loses the greedy
process, not the VM.

Light jobs ask for `[self-hosted, linux, x64, homelab-fc-light]` instead (Auto Kaizen's gate, merge and Watcher). They run in five light slots of their own, numbered after the
build slots (`/etc/homelab-ci/light-slots`, 0 for none): 1 vCPU, 2 GiB, no cache disk, a 3-hour
timeout (a merge waits for CI), and a light standby of their own (see Design). A light VM registers without `homelab-fc`, so it never takes a build job, and a
merge waiting on CI never takes the slot that CI needs. Memory goes to the oldest job first across both kinds: a
build waiting for memory holds back only the light jobs queued after it, and the log says why each one waits.

A VM starts only when the host's available memory (`MemAvailable` in
`/proc/meminfo`), less the VM's size, still leaves 3 GiB for the home's own
services; `/etc/homelab-ci/memory-reserve-mib` changes that amount. A booted VM
touches far less than its size and gives file cache back: `ci-trim-cache` in the
VM drops its cache once it passes a quarter of its memory, and the balloon
device's free page reporting returns the freed pages to the host. So this counts
what is really free, not the sizes added up. When a VM ends, the journal says how
much memory it used at most (`slot N: VM ended (exit 0), its memory peaked at X
MiB of Y`), to size VMs by kind of job. A VM never swaps: its memory cap (guest RAM plus 256 MiB) also counts
the host's cache of its disk, which goes first (`run-vm.sh`). No VM starts either while tasks waited on the disk for
over half of the last minute (`some avg60` in `/proc/pressure/io`), and over a quarter, one
a minute; the reason is beside `DISK_WAIT_LIMIT` in the supervisor. A job without the memory
or the disk stays queued and is tried again on the
next poll; the log says so once per job. A standby gives way first: none boots
while a job waits, and one waiting for another repo is stopped to
make room.

Only Colin's private repositories the token can see are served (its repository
access is the list; `served_repos` in the supervisor), and only those nobody else can
write to: a repo with any other collaborator or a deploy key that can write is skipped
(checked every minute and before its first VM), so only Colin
and his own apps (Kaizen, Renovate) can start a job here. Repos that have had a job here
are polled every 2 s (a standby booted for a repo does not count); the rest once a minute,
a few per poll, and the minutely repo listing runs beside the poll, so neither delays a
queued job's VM. The polled repos are asked all at once, each on its own thread over a
connection kept open, so a poll takes about as long as one repo's answers. A poll slower than 5 s is
logged (`slow poll:`) with its GitHub requests and its slowest repos. Only while private: privacy is
checked on every poll, and once a repo is public no new VM starts for it and its
idle ones are stopped. Before making a repo public, move its workflows to
GitHub-hosted runners and take it off the token's repository access.

## Design

- **One VM per job, never reused.** The supervisor polls GitHub every 2
  seconds (unchanged answers are free) for queued jobs with the label and boots
  a VM for each, up to two at once. The VM's disks are device-mapper snapshots of the images, not copies
  (`ci-runner/vm-disks.sh`): it starts in seconds, reads the image from the
  page cache all VMs share, and its writes go to a deleted file thrown away
  with everything the job wrote. A starting VM covers a queued job of its repo and
  label only until GitHub shows its runner with a job, for at most 2 minutes, so a
  second job of the same kind gets its own VM within a poll. The supervisor runs from
  the host's copy: re-run `sudo ci-runner/install-host.sh` to put a change to
  `scripts/ci_supervisor.py` on the Beelink (see
  [Changing the Beelink while jobs run](#changing-the-beelink-while-jobs-run)).
  However the job ends, the VM ends, within seconds. A VM left
  without a job (cancelled before it started, or taken by another runner) is
  stopped about 30 to 40 seconds after it boots.
- **One VM waits ready.** A standby VM is kept booted and registered for the
  repo and cache label of the last build job, with that label's cache disk (both
  kept in `/var/lib/homelab-ci/last-repo`, so a restart puts it back there), so
  the next job asking for the same starts in about a
  second instead of waiting for the next poll, a VM to boot and its runner to start
  (about 11 seconds). It costs about 450 MB of RAM while idle, and always has 4 vCPUs.
  Once it takes a job it is an ordinary job VM and a new standby boots in the
  free slot. A build job wanting another repo or label, with no slot free, stops
  it; an idle standby is replaced every 45 minutes, and stopped if its
  repo stops being private. `/etc/homelab-ci/standbys` sets how many wait (1 by
  default); 2 was measured and not worth its RAM (see the performance doc).
- **One light VM waits ready.** With a light slot free, a light standby waits
  registered for the repo of the last light job (kept in
  `/var/lib/homelab-ci/last-light-repo`; that repo is polled every 2 s), so a task's
  first and last steps start in seconds. It gives way first: none boots while a job
  waits for memory or the disk, and an idle one is stopped for another repo's light job with no
  light slot free or for a build job waiting for memory that was queued before its own light jobs. It is replaced every 45
  minutes like the standby. `/etc/homelab-ci/light-standbys` sets how many wait (1 by
  default, 0 for none).
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
sudo ci-runner/tests/test-tools.sh  # the image's tools run for a job; supervisor stopped first
sudo ci-runner/tests/audit.sh    # must pass before the supervisor runs
sudo systemctl enable --now homelab-ci-supervisor.service
journalctl -u homelab-ci-supervisor -f   # and each VM's own lines: journalctl -t homelab-ci-vm -f
```

`install-host.sh` also trims the SSD hourly instead of weekly: the VMs' throwaway write files churn about
0.5 TB a day, and untrimmed they slow every write (`systemd/fstrim-hourly.conf`).

`/etc/homelab-ci/slots` holds how many VMs run at once (5 when the file is
missing or not a number). `install-host.sh` writes it, keeping its value unless
run as `sudo SLOTS=4 ci-runner/install-host.sh`, and makes that many slot users
and taps; the supervisor reads it when it starts, and `setup-net.sh` and
`test-load.sh` read it too (a `SLOTS` variable wins). `/etc/homelab-ci/light-slots`
works the same way (`LIGHT_SLOTS`, 5 by default; the supervisor counts none until the file exists), and the users
and taps cover both. Change either only through `install-host.sh`, so the network always has a tap for every slot.
With N build slots the VMs use up to 2 + 2N GiB on paper (12 GiB for 5), plus 2 GiB per light slot (22 GiB for
5 + 5); light VMs mostly wait and the oldest job gets memory first. A job touches far less
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
 "light_slots": 5, "light_in_use": 2, "light_standby": 1, "cache_disks": ["ColinCee/example"], "github_ok": true,
 "mem_available_mib": 9216, "held_for_memory": 0}
```

`in_use` counts build VMs started for a job, `standby` the ones waiting ready,
`light_slots`, `light_in_use` and `light_standby` the light slots, the light VMs
running a job and the ones waiting ready,
`cache_disks` the repos with a disk in the caches folder, and `github_ok` is
false when the last poll failed or was rate limited. `mem_available_mib` is the
host's free memory when the file was written, and `held_for_memory` counts queued
jobs not started because that memory would have dipped into the services' reserve, or the disk was busy. A failed write is logged
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

The same reader saves each `KAIZEN-SESSION 1 <JSON object>` line (printable ASCII only)
from an auto-kaizen VM to `sessions/<runner name>.jsonl` in that folder: the agents'
whole sessions, for the Control room's live chat page (auto-kaizen's
`docs/live-session.md`). The file is named by the VM the line came from, never by the
line, so one VM cannot write into another's. Folder 0750 and files 0640, root only, since
sessions can carry private notes. A file stops growing at 30 MB, and files not written to
for 30 days are deleted. A failed save is logged once and the console keeps being read.

Re-run `build-rootfs.sh` after a change under `ci-runner/rootfs/` and after a kernel upgrade, since the VMs boot the host's
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
  mise, bun, pnpm, `docker compose`, `docker buildx`, python3, shellcheck, `file`, gh, gitleaks,
  `bwrap`, socat) as the runner user with the job's `PATH`,
  then builds a small image with Docker (BuildKit's `RUN --mount`), runs it with a published port and fetches its page.
  Run it after `build-rootfs.sh`, with the supervisor stopped
  (`sudo systemctl stop homelab-ci-supervisor`) and its VMs finished (VMs outlive the
  supervisor: wait for them, or stop them with `sudo ci-runner/stop-vms.sh`): it boots
  in slot 0, and fails with "slot 0 is busy" while a job holds it.
- **`test-load.sh`** boots a VM in every slot (the count in
  `/etc/homelab-ci/slots`) at the size a job gets while others run (2 vCPUs,
  2 GiB), each spinning every CPU and holding all but 128 MiB of its free
  memory, while timing Home Assistant and Grafana once a second. It prints each
  service's median and worst response with the second it came (counted from
  when the VMs start), each failed request as `code@second`, the host's lowest
  `MemAvailable` and most data waiting to be written to disk during the run, then one `PASS` or `FAIL` line naming the slot
  count. It fails, exiting 1 with the reasons on that line, when a VM does not
  boot or finish its burn, any request fails, or a worst response is over
  `MAX_RESPONSE` seconds (1 by default). On 4 November 2026, before the VMs'
  CPU weight moved to their shared cgroup, Grafana's worst was 0.7 to 3.8 s at
  3 to 5 slots, with 5 failed requests in some runs. With that fixed, Grafana
  still answered 503 20 to 50 s after the VMs started, at any slot count, most
  likely while the kernel wrote out the image copies. VMs now boot from
  snapshots of the images instead of copies (5 Nov), and the loaded timings
  start when the VMs start.

## Changing the Beelink while jobs run

Every change goes in through `sudo REASON="<repo>#<number>: <why>" ci-runner/install-host.sh`, naming the issue or pull request it ships (the Control room links it),
from a checkout of `main`. Never leave a loop waiting to change the Beelink from your
own connection: on 5 Nov one outlived the session that thought it had stopped it.

- **Running jobs carry on through any change.** Each VM is a systemd unit of its own
  (`homelab-ci-vm-<runner name>`), not the supervisor's child, with its record in
  `/run/homelab-ci/vms/`. A restarted supervisor takes the running VMs back and carries
  on watching them; a restart takes seconds and cuts off nothing (on 5 Nov, before this,
  three restarts cut off 20 running jobs).
- **Without a restart:** `run-vm.sh`, `vm-disks.sh`, `stop-vms.sh`, Firecracker and
  a rebuilt VM image (`build-rootfs.sh`) go in while jobs run. New VMs get them;
  running ones keep what they started with.
- **With a restart:** a change to the supervisor, the network rules, the systemd
  units or the slot counts restarts the supervisor. The rules reload together with
  the home's public address in one transaction, so running VMs never see that address
  unblocked.
- **One at a time:** a second `install-host.sh` waits for the first and names it.
- **Stopping every VM** on purpose (to clear the host): `sudo ci-runner/stop-vms.sh`.
- **A VM's own lines** (its errors and memory peak) are in `journalctl -t homelab-ci-vm`.

## Changing the number of slots

With the supervisor stopped (`sudo systemctl stop homelab-ci-supervisor`) and
its VMs finished (they outlive the supervisor: wait for them, or stop them with
`sudo ci-runner/stop-vms.sh`):

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
