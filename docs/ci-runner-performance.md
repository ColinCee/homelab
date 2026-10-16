# How fast is personal CI in Firecracker microVMs?

Measured in October 2026 on the homelab's mini PC (Intel N100: 4 cores,
4 threads, 16 GB RAM, NVMe), running CI for one private Python and TypeScript
repository (cortex: `uv sync`, `npm ci`, and a pytest suite of about 280 tests
that also runs ruff, ty and tsc). How the runner is built is in
[`runbooks/ci-runner.md`](runbooks/ci-runner.md).

Unless a section says otherwise: one job at a time, 5 runs of each setup
interleaved with the others, GitHub's step timestamps (whole seconds), medians
with ranges.

## The short answer

- **20 seconds from GitHub creating the run to done, against 26 to 29 on
  GitHub's `ubuntu-latest`**, the same cortex commit (e016c52) on both. The job
  is running about 3 seconds after the run is created: GitHub takes most of
  that, and a VM kept booted and waiting takes the job straight away.
- **Firecracker costs about 4 seconds per job against no sandbox at all**
  (14.3 s against 10.7 s for the same job, offline, warm caches): about 0.8 s
  to boot and the rest from running on virtual CPUs.
- **No GitHub minutes.** Private repositories get 2,000 free minutes a month on
  GitHub's runners; these jobs use none.
- **Home Assistant and Grafana did not notice.** With two VMs flat out on CPU
  and memory, their worst response time went from 1 ms to 4 ms.

## Each change, measured on one commit

Every stage of the runner rebuilt and measured in one session on cortex
e016c52, Python 3.15, interleaved with GitHub's runner. Isolation is the same
in every stage (one throwaway VM per job, a one-job registration, the token on
the host, the same network rules); only the speed changes differ.

| Stage | Run created → running | Job | Run created → done |
|---|---|---|---|
| GitHub `ubuntu-latest`, its own caches | 4 s (4–4) | 22 s (20–28) | 26 s (24–32) |
| First version: polls every 10 s, boots a VM per job, nothing in the image, GitHub's caches | 14 s (13–14) | 37 s (37–39) | **51 s (50–52)** |
| + Node, uv and Python in the image, polling every 2 s | 15 s (12–16) | 32 s (32–33) | **48 s (44–48)** |
| + one VM kept booted and waiting | 3 s (3–4) | 34 s (32–35) | **37 s (35–38)** |
| + a disk of main's caches instead of GitHub's caches | 3 s (3–4) | 25 s (24–26) | **28 s (27–29)** |
| + everything else front-loaded (today) | 3 s (3–4) | 17 s (17–19) | **20 s (20–22)** |

Per step (seconds):

| Stage | Set up job | Checkout | setup-uv | setup-node | `uv sync` | `npm ci` | Tests |
|---|---|---|---|---|---|---|---|
| GitHub | 2 | 1 | 2 | 2 | 1 | 2 | 8 |
| First version | 3 | 2 | 4 | 11 | 2 | 3 | 7 |
| + tools | 2 | 2 | 4 | 5 | 1 | 3 | 9 |
| + waiting VM | 3 | 2 | 5 | 5 | 2 | 3 | 8 |
| + cache disk | 3 | 1 | 1 | 1 | 2 | 3 | 8 |
| Today | 0 | 2 | – | – | 2 (one install step) | | 9 |

- **The waiting VM is the biggest single step (11 s):** without it a job waits
  for the next poll, a boot and the GitHub runner starting inside the VM. It
  holds about 450 MB of RAM while it waits.
- **Tools in the image save the downloads** (`setup-node` 11 s → 5 s), but the
  setup actions still cost 4–5 s each while they restore GitHub's caches.
- **The cache disk removes those restores** (9 s). It is built by a green push
  to main, which pays about 20 s more to upload it.
- **The last stage** (8 s) is the actions unpacked in the image, no setup
  actions, `node_modules` copied from the cache disk when the lockfile matches
  main's, and `uv sync --no-install-project`.
- The tests are 7–9 s throughout: the same code on the same CPU.

A second like-for-like round an hour later, also on e016c52, gave GitHub 29 s
(26–36) and the home runner 20 s (20–21). GitHub's runner varies more.

## What was tried and not kept

Each against a control in the same session:

| Idea | Result |
|---|---|
| Full Firecracker snapshot of a VM after a main build, restored as the waiting VM | ~1 s, all of it `uv sync`, which `--no-install-project` gets without one |
| Two waiting VMs instead of one | a burst of two pushes finishes 4 s sooner, average per job unchanged, 440 MB more held |
| 4 vCPUs instead of 2 for a lone job | no change (kept, harmless) |
| `node_modules` linked instead of copied | the TypeScript check 3.6 s → 8.4 s: its incremental build info is relative to where it lives |
| `node_modules` bind-mounted | install 1 s faster, tests 1 s slower; needs root in the job |
| `mitigations=off` in the guest kernel | within noise |
| Reading the environment into memory while the VM waits | tests 2–3 s slower: the reading competed with the job |
| Compiling cortex's bytecode before the tests | install +1 s, tests unchanged |
| pytest worker counts and options, `persist-credentials: false` | no change |

Kept in cortex itself: TypeScript 7 (the type check 3.6 s → 1.3 s) and running
the slow checks first, so they overlap the rest of the suite.

## Why snapshots did not help

Firecracker restores a snapshot in milliseconds (13 to 113 ms for a 2 GiB VM in
the VM-level comparison; 11 to 15 ms for the 4 GiB waiting VM in the whole-job
round), but boot was never the
problem: the waiting VM already hides it, along with the GitHub runner's
start-up, which a snapshot cannot include (the one-job registration only
exists once a job is queued). In a VM-level comparison, a plain disk of main's
installs and caches got 17.2 s of work and a full snapshot 16.9 s: almost all of
the gain is files on disk, not memory. A snapshot also costs 30 s to take, a
memory file the size of the VM per slot (1.2 GB for the 2 GiB VM, 4.1 GB for
the 4 GiB one), and every restored VM resumes with
identical memory, random-number state included, which Firecracker's
documentation warns against.

## Sandboxes compared

The same job (offline, dependencies already cached) under each kind of
isolation on the same machine. "Start" is what each adds before the job runs.

| Isolation | Job | Start | Peak RAM | Own kernel |
|---|---|---|---|---|
| None (a dedicated user) | 10.7 s | 0.1 s | 0.5 GB | no |
| systemd hardening | 10.6 s | 0.1 s | 0.5 GB | no |
| firejail | 10.3 s | 0.1 s | 0.5 GB | no |
| systemd-nspawn | 10.4 s | 0.5 s | 0.9 GB | no |
| Docker | 11.3 s | 0.3 s | 0.5 GB | no |
| Rootless Podman | 10.7 s | 0.3 s | 0.4 GB | no |
| Incus system container | 19.0 s | 5.0 s | 0.6 GB | no |
| Incus (QEMU) VM | 39.5 s | 9.8 s | 1.5 GB | yes |
| **Firecracker microVM** | **14.3 s** | **0.8 s** | 1.1 GB | **yes** |

Firecracker is the only option with its own kernel that stays close to bare
speed. The containers are faster but share the host's kernel, and most could
reach other services on the network without a firewall added as well.

## Throughput, cores and power

Four jobs back to back, offline:

| Shape | Jobs/hour | Per job under load | Peak RAM |
|---|---|---|---|
| No sandbox, 1 at a time | 340 | 10.5 s | 0.3 GB |
| Firecracker 4 vCPU, 1 at a time | 258 | 13.9 s | 0.6 GB |
| Firecracker 2 vCPU, 2 at once | 378 | 18.9 s | 1.5 GB |
| Firecracker 2 vCPU, 4 at once | 415 | 34.6 s | 3.5 GB |

The suite in one 4-vCPU VM by number of test workers: 1: 10.5 s, 2: 7.4 s,
3: 7.0 s, 4: 6.7 s, 6: 7.4 s. About 5 s cannot be split up (Python starting,
collecting the tests, the single TypeScript check), so more cores would barely
help; faster cores would.

The host CPU's energy preference (intel_pstate, turbo on), measured with the
CPU package's own energy counter (RAPL, not the whole box at the wall):

| Setting | Run created → done | CPU energy per job | Idle CPU power |
|---|---|---|---|
| `performance` | 21 s | 246 J | 2.29 W |
| **`balance_performance` (default, kept)** | **20–21 s** | 226 J | **0.77 W** |
| `balance_power` | 24 s | 167 J | 0.71 W |
| `power` | 35.5 s | 132 J | 0.68 W |

The default is the best fit: `performance` is no faster and triples idle power,
and the saving modes save under 0.1 W at idle, where the box spends almost all
its time.

## Lessons

- **Measure on one commit, in one session, against GitHub's runner.** Earlier
  rounds on different commits gave GitHub anywhere from 26 s to 35 s, because
  every cortex change sped GitHub up too.
- **When the comparison changes, re-run all of it**, not just the new row.
- **Check that a speed-up is really in use.** The first cache disk needed
  OverlayFS, which the VM's kernel did not have, so every "warm" job ran cold
  while the timings looked normal; only the job's own log showed it. A first
  snapshot benchmark looked 20 times faster because every run after the first
  failed in under a second; only checking each exit code caught it.
- **Take the per-job fixed costs out first** (waiting, booting, downloading,
  restoring). What is left is the tests, which only faster tests or faster
  cores can shorten.

## Things that surprised me

- **A job that ran out of memory hung its VM for an hour.** systemd stopped the
  task meant to end the VM. The VM now reboots, which ends Firecracker,
  whenever its task ends, however it ends.
- **`sudo` hid Node from the jobs.** `sudo -u runner` resets `PATH`, so the
  runner's `.path` file never applied. The runner is now started with that
  `PATH` explicitly.
- **GitHub only saves a cache when the job passes,** so the package caches did
  nothing until the suite was green on Linux.
- **CI on a second Python found a real bug.** One test faked "today" by
  replacing `time.time`, which `date.today()` no longer reads in Python 3.15.
  Another, replacing `time.sleep`, hung for 11 minutes on GitHub's runner,
  because `Popen.wait` polls with it on Linux before 3.15.
