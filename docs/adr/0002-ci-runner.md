# 2. CI runner

## Status

Accepted.

## Context

Colin's private repositories needed CI without GitHub's billed minutes, on the Beelink, without a job reaching the
home's services or the next job. Measured in October 2026 on the Beelink (Intel N100: 4 cores, 16 GB, NVMe) with one
private Python and TypeScript repository's CI (`uv sync`, `npm ci`, about 280 tests): one job at a time, 5 runs of
each setup interleaved, medians.

The same job, offline with warm caches, under each isolation:

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

## Decision

A Firecracker microVM per job, never reused, jailed as its slot's own user, with VMs kept booted and waiting and a
disk of the default branch's caches. It is the only option with its own kernel that stays near bare speed.

From GitHub creating the run to done, on the same commit:

| Setup | Run created → running | Run created → done |
|---|---|---|
| GitHub `ubuntu-latest`, its own caches | 4 s | 26 s |
| First version: a VM per job, polling every 10 s, GitHub's caches | 14 s | 51 s |
| + tools in the image, polling every 2 s | 15 s | 48 s |
| + one VM kept booted and waiting | 3 s | 37 s |
| + a cache disk of main's caches | 3 s | 28 s |
| + everything else front-loaded | 3 s | 20 s |

## Consequences

- Faster than GitHub's runners: 20 s against 26 to 29 s.
- Firecracker costs about 4 s a job against no sandbox: 0.8 s to boot, the rest from virtual CPUs.
- With two VMs flat out on CPU and memory, Home Assistant's and Grafana's worst response went from 1 ms to 4 ms.
