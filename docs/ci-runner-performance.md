# How fast is personal CI in Firecracker microVMs?

Measured in October 2026 on Beelink, a Beelink EQ mini PC (Intel N100: 4 cores,
4 threads, 16 GB RAM, NVMe), running CI for one private Python and TypeScript
repository (cortex: `uv sync`, `npm ci`, and a pytest suite of about 280 tests
that also runs ruff, ty and tsc). How the runner is built is in
[`runbooks/ci-runner.md`](runbooks/ci-runner.md).

## The short answer

- **From GitHub creating the run to done in 21 seconds; the job itself 18.**
  It is running about 3 seconds after the run is created: GitHub takes most of
  that, and a VM kept booted and waiting takes the job straight away. (Medians
  of 5, same session as the GitHub comparison below.)
- **Firecracker costs about 4 seconds per job against no sandbox at all** (14.3 s
  against 10.5 s for the same job, offline, warm caches): about 0.8 s to boot
  and the rest from running on virtual CPUs.
- **Faster than GitHub's own runner.** The same job, Python 3.15 on both, five
  runs each, interleaved: 21 s here against 35 s on `ubuntu-latest` from run
  created to done, and 18 s against 30 s for the job (medians). See "Squeezing
  it" below for how.
- **No GitHub minutes.** Private repositories get 2,000 free minutes a month on
  GitHub's runners; these jobs use none.
- **Home Assistant and Grafana did not notice.** With two VMs flat out on CPU
  and memory, their worst response time went from 1 ms to 4 ms.

## Sandboxes compared

The same job (the cortex check, offline, dependencies already cached) under
each kind of isolation on the same machine. "Start" is what each adds before
the job runs.

| Isolation | Job | Start | Peak RAM | Kernel shared with the host |
|---|---|---|---|---|
| None (a dedicated user) | 10.7 s | 0.1 s | 0.5 GB | yes |
| systemd hardening | 10.6 s | 0.1 s | 0.5 GB | yes |
| firejail | 10.3 s | 0.1 s | 0.5 GB | yes |
| systemd-nspawn | 10.4 s | 0.5 s | 0.9 GB | yes |
| Docker | 11.3 s | 0.3 s | 0.5 GB | yes |
| Rootless Podman | 10.7 s | 0.3 s | 0.4 GB | yes |
| Incus system container | 19.0 s | 5.0 s | 0.6 GB | yes |
| Incus (QEMU) VM | 39.5 s | 9.8 s | 1.5 GB | no |
| **Firecracker microVM** | **14.3 s** | **0.8 s** | 1.1 GB | **no** |

Firecracker is the only option with its own kernel that stays close to bare
speed: a full QEMU VM under Incus was almost three times slower, mostly in
boot and disk. The containers are faster but share the host's kernel, and most
of them could reach other services on the network unless a firewall was added
as well.

## Throughput

Four jobs back to back, offline:

| Shape | Jobs/hour | Per job under load | Peak RAM |
|---|---|---|---|
| No sandbox, 1 at a time | 340 | 10.5 s | 0.3 GB |
| No sandbox, 2 at once | 469 | 14.8 s | 0.9 GB |
| Firecracker 4 vCPU, 1 at a time | 258 | 13.9 s | 0.6 GB |
| Firecracker 2 vCPU, 2 at once | 378 | 18.9 s | 1.5 GB |
| Firecracker 2 vCPU, 4 at once | 415 | 34.6 s | 3.5 GB |

Two VMs at once is the sweet spot on four cores: 378 jobs an hour, or roughly
what 2,000 GitHub minutes buy in a month, every 5 hours.

## A real push, step by step

From GitHub creating the run to the job finishing, as the setup improved:

| Version | Run created → job running | Run created → done |
|---|---|---|
| First working version (poll every 10 s, tools downloaded each job) | 12 s | 52 s |
| Node, uv and Python built into the VM image, poll every 2 s | 12 s | 44 s |
| One VM kept booted and waiting | 3 to 4 s | 37 s |
| Plus a warm cache disk from main | 3 s | 33 s |

Where the 33 seconds of a job now go:

| Step | Time |
|---|---|
| Set up job (the runner downloads the actions) | 2 to 3 s |
| Checkout | 1 s |
| setup-uv, setup-node (restoring GitHub's package cache) | 5 to 6 s |
| `uv sync`, `npm ci` | 4 to 5 s |
| Tests | 13 s |

## Against GitHub's own runner

The cortex check on `ubuntu-latest` (2 vCPUs, 7 GB) and on this runner, five
runs each, one job at a time, Python 3.15 on both (GitHub's image has 3.12, so
uv downloads 3.15 there). Medians, in seconds:

| Step | GitHub | Here |
|---|---|---|
| Set up job, checkout, setup-uv | 5 | 6 |
| setup-node | 3 | 4 |
| `uv sync`, `npm ci` | 5 | 4 |
| Tests | 15 | 16 |
| **Whole job** | **34** (26 to 37) | **36** (36 to 42) |

Level, within the spread. Two vCPUs of a server CPU keep up with the N100's
four cores, and the Firecracker overhead (about 4 s, above) is roughly the gap.
GitHub picks a job up in about 3 seconds too. The repository's nightly
Windows check on GitHub takes about a minute and a half.

## What made a difference, and what did not

- **Building the tools into the image:** `setup-node` went from 7 s to 1 s and
  `uv sync` from 3 s to 1 s, because nothing is downloaded per job.
- **Keeping one VM booted:** saves about 8 seconds, almost all of it the
  GitHub runner starting up inside the VM, not the VM booting. It costs about
  450 MB of RAM while it waits.
- **Polling every 2 seconds instead of 10:** free, because GitHub does not
  count a conditional request whose answer has not changed.
- **4 vCPUs instead of 2 for a job running alone:** did nothing for this
  suite. Half the tests finish in under 2 seconds and a few slow ones decide
  the time.
- **Firecracker snapshots:** not used. Boot is already under a second, and
  restoring the same snapshot for every job is unsafe (Firecracker's own
  documentation says so).

## Snapshots against a warm cache disk

Firecracker can snapshot a running VM and restore it in milliseconds, so the
obvious idea is to snapshot a VM right after a build of main and restore it
for every job. I measured that against the alternatives on the same workload
(`uv sync`, `npm ci`, the test suite), three runs each, medians, in a VM with 2
vCPUs. "Work" is the time inside the VM; "start" is boot or restore.

| Setup | Start | `uv sync` | `npm ci` | Tests | Work |
|---|---|---|---|---|---|
| Fresh VM, empty caches | ~3.5 s boot | 1.9 s | 4.7 s | 15.0 s | 21.5 s |
| Fresh VM, warm package caches | ~3.5 s boot | 0.4 s | 3.6 s | 15.0 s | 19.3 s |
| Fresh VM, warm caches and main's installs | ~3 s boot | 0.1 s | 3.6 s | 13.4 s | 17.2 s |
| Snapshot of main, installs wiped | 0.02 to 0.1 s restore | 1.4 s | 4.0 s | 14.2 s | 19.9 s |
| Snapshot of main, installs kept | 0.02 to 0.1 s restore | 0.1 s | 3.5 s | 13.0 s | 16.9 s |

- **The restore really is fast:** 13 to 113 ms. But boot was never the
  problem: the standby VM already hides it, along with the GitHub runner's
  start-up, which a snapshot cannot include (the runner's one-job registration
  only exists once a job is queued).
- **The snapshot's memory is worth about 0.3 s.** A plain disk holding main's
  installs and caches gets 17.2 s; the full snapshot 16.9 s. Almost all of the
  gain is files on disk, not memory.
- **Warm memory is not free on first touch.** Restored memory is read from the
  snapshot file as it is used, so with the installs wiped the snapshot was
  slower than a cache disk.
- **`npm ci` stays at about 3.5 s whatever is warm,** because it deletes
  `node_modules` before installing, by design.
- **What a snapshot costs:** 30 s to take, a 1.2 GB memory file per slot (the
  network address is baked in), and every restored VM resumes with identical
  memory, random-number state included, which Firecracker's documentation
  warns against.

So the speed-up worth having is a disk of main's caches and installs, built in
a VM from main only. Snapshots would add 0.3 s.

## The cache disk, measured as whole jobs

Built and running since 15 October 2026 (see the runbook's "Cache disk"). Five
real cortex jobs each, alternating, one at a time, GitHub's step timestamps
(whole seconds), medians with ranges:

| | Today's setup (GitHub's cache) | Warm cache disk |
|---|---|---|
| Run created → job running | 3 s (3 to 4) | 3 s (3 to 5) |
| Job | 34 s (33 to 34) | **30 s (30 to 30)** |
| Run created → done | 37 s (36 to 38) | **33 s (33 to 35)** |
| Set up job, checkout | 4 s | 5 s |
| `setup-uv` + `setup-node` (cache restore) | 6 s | 1 s |
| `uv sync` | 1 s | 2 s |
| `npm ci` | 3 s | 3 s |
| Tests | 13 s | 13 s |

It saves 4 seconds a job, about half what the in-VM experiment suggested.
Almost all of it is not restoring GitHub's caches. `uv sync` is a little
slower, because it rebuilds the project itself into the environment on every
job (a fresh checkout looks like new source); `npm ci` reinstalls
`node_modules` whatever is cached, by design; and the tests are the same CPU
work either way. A green push to main pays about 20 seconds more, to upload
the new cache.

## Squeezing it, measured against GitHub's runner

Same session, early on 16 October 2026: cortex's Linux check, Python 3.15, one
job at a time, 5 runs of each setup interleaved, GitHub's step timestamps
(whole seconds), medians with ranges. "GitHub" is the same job on GitHub's
`ubuntu-latest` runner with its own caches; uv fetches Python 3.15 there.

| Setup | Run created → job running | Job | Run created → done |
|---|---|---|---|
| GitHub `ubuntu-latest` | 5 s (4–5) | 30 s (25–32) | **35 s (30–37)** |
| Beelink, cache disk (previous workflow) | 3 s (3–4) | 28 s (28–28) | 31 s (31–32) |
| Beelink, full snapshot of a VM after a main build | 3 s (3–4) | 28 s (28–29) | 32 s (31–32) |
| **Beelink, final workflow** | 3 s (3–4) | **18 s (18–19)** | **21 s (21–23)** |

(The snapshot row is from an earlier round of the same session, against a cache
disk control of 33 s.)

Everything tried, in order, with what it changed:

| Idea | Before → after | Kept |
|---|---|---|
| Actions unpacked in the VM image (`ACTIONS_RUNNER_ACTION_ARCHIVE_CACHE`) | "Set up job" 3 s → 1 s | yes |
| No `setup-uv` / `setup-node` (tools on the job's `PATH` in the image) | ~1 s of steps and post-steps → 0 | yes |
| `node_modules` copied from the cache disk when the lockfile matches main's | `npm ci` 3–4 s → part of a 1–2 s install step | yes |
| `uv sync --no-install-project` (cortex is not rebuilt into the environment) | `uv sync` 2 s → part of the same step | yes |
| `node_modules` linked instead of copied | the TypeScript check went from 3.6 s to 8.4 s: its incremental build info is relative to where it lives, and a link moves it | no |
| Slow checks first (`tsc`, `ty`, `ruff` start before the rest, so they overlap it) | tests 9.0 s → 7.9 s | yes |
| Compiling cortex's bytecode once before the tests | install +1 s, tests unchanged | no |
| Full Firecracker snapshot as the waiting VM | ~1 s, all of it `uv sync`, which `--no-install-project` gets without one | no |
| Two waiting VMs instead of one | a burst of two pushes: last job done 33 s → 29 s, average per job unchanged (27.5 s → 28 s), 440 MB more held | no |
| 4 vCPUs instead of 2 for a lone job | no change (earlier) | kept, harmless |

Not tried, because the most each could save is under a second: checking out
from a clone on the cache disk (checkout is 1–2 s, mostly the network), and
polling more often than every 2 seconds (the 3 s to start is mostly GitHub
creating the run).

Where the 18-second job goes now: about 1 s setting up, 1 s checkout, 2 s
install, and 9–10 s of tests, of which the TypeScript check is 3.6 s (most of it
the compiler starting), pytest's start-up and collection about 2.5 s, and the
other 280 tests run alongside on the other cores. The rest is GitHub's own
bookkeeping between steps. Going lower means faster tests, which is cortex's
work, or a faster CPU: GitHub's runner spends 10 to 15 s on the same tests.

From the morning to now, run created → done went 52 s → 37 s → 33 s → 21 s.

## Things that surprised me

- **A job that ran out of memory hung its VM for an hour.** systemd stopped the
  task that was meant to end the VM. The fix: the VM reboots, which ends
  Firecracker, whenever its task ends, however it ends.
- **`sudo` hid Node from the jobs.** The VM starts the runner with
  `sudo -u runner`, which resets `PATH` to its own default, so the runner's
  `.path` file never applied and the TypeScript test failed with "node is not
  on PATH". The runner is now started with that `PATH` explicitly.
- **The first cache disk was attached and never used.** Its throwaway write
  layer needed OverlayFS, which Ubuntu's kernel builds as a module, and the VM
  has no modules, so every "warm" job started from an empty cache while the
  timings looked normal. Only reading the job's own log (`uv` downloading
  packages it should have had) showed it. Each VM now gets a private copy of
  the disk instead.
- **My first snapshot benchmark looked 20 times faster, and was wrong.** I had
  copied a VM's disk while it was still mounted, every run after it failed in
  under a second, and the timings looked brilliant. Only checking each step's
  exit code caught it.
- **GitHub only saves a cache when the job passes,** so the package caches did
  nothing until the suite was green on Linux.
- **The VM ran Python 3.15, the PC 3.13.** One test faked "today" by replacing
  `time.time`, which `date.today()` stopped reading in 3.15. CI on a second
  platform found a bug that would have appeared on the PC at the next Python
  upgrade.
- **On GitHub's runner the suite hung for 11 minutes.** Tests that step a loop
  by replacing `time.sleep` also replaced the sleep that `Popen.wait` polls
  with on Linux before Python 3.15, so one test lost its steps and the next
  spun for ever. Python 3.15 waits on the process instead, so the VM never saw
  it.
