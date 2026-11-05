# Architecture

How changes reach the Beelink, and who can reach what runs on it. Auto Kaizen, the factory that makes many of these changes, has its own: [auto-kaizen architecture](https://github.com/ColinCee/auto-kaizen/blob/main/docs/architecture.md).

```mermaid
flowchart TB
  subgraph GitHub
    pr[Pull request] --> ci[CI, on GitHub's runners]
    ci --> main[Merge to main]
    watchdog[Deploy watchdog, after each deploy]
    responder[Kaizen's responder, in Kaizen's VMs]
  end
  subgraph Beelink["Beelink (Ubuntu, Docker)"]
    guard{Runner guard}
    deploy[Deploy runner]
    stacks[Compose stacks]
    cirunner[CI runner: throwaway Firecracker VMs, no LAN]
  end
  colin[Colin] -->|merges| main
  renovate[Renovate] -->|merges version bumps| main
  kaizen[Auto Kaizen] -->|merges what passed its checks| main
  main --> guard -->|Colin, Renovate, Auto Kaizen| deploy --> stacks
  main -.-> watchdog
  watchdog -.->|failed or blocked deploy| kaizen
  watchdog -.->|stuck deploy| discord
  privaterepos[Private repos' CI] --> cirunner
  you[Colin, via Tailscale] --> stacks
  public[Public, via Cloudflare Tunnel] --> stacks
  stacks -->|nightly backups| r2[Cloudflare R2]
  stacks -->|Kaizen's own failures| discord[Discord]
  stacks -->|alerts, via the Control room| responder
  stacks -->|one post per alert, from the Control room| discord
  responder -->|Remediate: look, restart a broken container| guard
  responder -->|task for the lasting fix| kaizen
```

- **Merging is deploying.** A merge to `main` runs [`deploy.yaml`](../.github/workflows/deploy.yaml) on the deploy runner, which renders each stack's `.env` from GitHub secrets and applies every stack. The [runner guard](../scripts/runner_job_guard.py) kills any job not started by Colin, Renovate or Auto Kaizen.
- **Two runners on one host.** The deploy runner holds the secrets and drives Docker, so only this repo's deploys run there. The [CI runner](runbooks/ci-runner.md) runs other private repos' CI in throwaway VMs that reach the internet but not the home network.
- **Auto Kaizen** merges only changes that pass CI, its reviewer and its red-line checks; changes to `scripts/`, `ci-runner/` and `.github/`, compose options that give a container power over the host, and a stack's `.env.example` asking for a deploy secret, wait for Colin. A failed or blocked deploy stops its work until fixed.
- **Alerts are answered by an agent with no key to the box.** Each new Grafana alert opens an `alert` issue in Auto Kaizen, whose responder runs in Kaizen's VMs, and gets one post in Discord's #alerts forum. It sees and touches Beelink only through [`remediate.yaml`](../.github/workflows/remediate.yaml), which the runner guard lets Auto Kaizen run beside Deploy and nothing else: a status report, and a restart of one container that is down or unhealthy. Lasting fixes become Kaizen tasks and reach Beelink only by merge. See [observability](observability.md#alerts).
- **Access.** Admin pages bind to the Tailscale address; only the flight tracker's API is public, through Cloudflare Tunnel.
