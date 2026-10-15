# How fast is personal CI in Firecracker microVMs?

Measured in October 2026 on Beelink, a Beelink EQ mini PC (Intel N100: 4 cores,
4 threads, 16 GB RAM, NVMe), running CI for one private Python and TypeScript
repository (cortex: `uv sync`, `npm ci`, and a pytest suite of about 280 tests
that also runs ruff, ty and tsc). How the runner is built is in
[`runbooks/ci-runner.md`](runbooks/ci-runner.md).

## The short answer

- **A push is a running job in about 4 seconds and a finished one in about 37.**
  GitHub takes about 3 of those seconds to create the run; a VM kept booted
  and waiting takes the job about a second later.
- **Firecracker costs about 4 seconds per job against no sandbox at all** (14.3 s
  against 10.5 s for the same job, offline, warm caches): about 0.8 s to boot
  and the rest from running on virtual CPUs.
- **As fast as GitHub's own runner.** The same job, Python 3.15 on both, five
  runs each: 36 s here against 34 s on `ubuntu-latest` (medians).
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

So the speed-up worth having is a read-only disk of main's caches and installs,
built in a VM from main only, which takes a job from about 21.5 s of work to
about 17 s. Snapshots would add 0.3 s.

## Things that surprised me

- **A job that ran out of memory hung its VM for an hour.** systemd stopped the
  task that was meant to end the VM. The fix: the VM reboots, which ends
  Firecracker, whenever its task ends, however it ends.
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
