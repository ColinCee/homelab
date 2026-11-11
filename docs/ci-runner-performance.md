# CI runner performance

How fast the [Firecracker CI runner](runbooks/ci-runner.md) is, measured in October 2026 on the Beelink
(Intel N100: 4 cores, 16 GB, NVMe) on one private Python and TypeScript repository's CI (`uv sync`, `npm ci`
and about 280 tests). One job at a time, 5 runs of each setup interleaved, GitHub's step timestamps, medians
with ranges.

## Short answer

- 20 s from GitHub creating the run to done, against 26 to 29 s on GitHub's `ubuntu-latest`, on the same
  commit. The job runs about 3 s after the run is created: a VM kept booted and waiting takes it at once.
- Firecracker costs about 4 s a job against no sandbox (14.3 s against 10.7 s, offline, warm caches): 0.8 s
  to boot, the rest from virtual CPUs.
- With two VMs flat out on CPU and memory, Home Assistant's and Grafana's worst response went from 1 ms to
  4 ms.

## Each stage, on one commit

| Stage | Run created → running | Job | Run created → done |
|---|---|---|---|
| GitHub `ubuntu-latest`, its own caches | 4 s (4–4) | 22 s (20–28) | 26 s (24–32) |
| First version: polls every 10 s, a VM per job, nothing in the image, GitHub's caches | 14 s (13–14) | 37 s (37–39) | 51 s (50–52) |
| + Node, uv and Python in the image, polling every 2 s | 15 s (12–16) | 32 s (32–33) | 48 s (44–48) |
| + one VM kept booted and waiting | 3 s (3–4) | 34 s (32–35) | 37 s (35–38) |
| + a disk of main's caches instead of GitHub's | 3 s (3–4) | 25 s (24–26) | 28 s (27–29) |
| + everything else front-loaded (today) | 3 s (3–4) | 17 s (17–19) | 20 s (20–22) |

## Sandboxes

The same job (offline, dependencies cached) under each kind of isolation on the Beelink. "Start" is what
each adds before the job runs.

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
| Firecracker microVM | 14.3 s | 0.8 s | 1.1 GB | yes |

Firecracker is the only option with its own kernel that stays close to bare speed.
