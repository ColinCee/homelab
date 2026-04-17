# Deploy Pipeline Migration: Dokploy → GitHub Actions

## Goal

Replace Dokploy with a GitHub Actions pipeline that:

1. Detects which stacks changed on push to `main`
2. Generates `.env` files from templates + GitHub secrets
3. SSHes to beelink and deploys (git pull + compose up)

Secrets live in GitHub (single source of truth), `.env.example` templates in the repo.

## What's been built (all committed to main)

### Workflow: `.github/workflows/deploy.yaml` (83 lines)

```
push to main → detect changed stacks → generate .env → tailscale connect → SSH deploy
```

- **Auto-detection**: `scripts/detect-stacks.sh` discovers stacks from `stacks/*/compose.yaml` (no hardcoded list)
- **Manual trigger**: `workflow_dispatch` with stack selector dropdown
- **Env generation**: `scripts/generate-env.sh` runs `envsubst` with explicit variable list from `.env.example`
- **Concurrency**: `cancel-in-progress: false` — deploys queue, never cancel mid-deploy

### Scripts (all in `scripts/`)

| Script | Purpose | Runs on |
|--------|---------|---------|
| `detect-stacks.sh` | Git diff → which stacks changed | GHA runner |
| `generate-env.sh` | `.env.example` + secrets → `.env` | GHA runner |
| `deploy.sh` | `git pull` + `docker compose up` per stack | beelink |
| `deploy-gate.sh` | SSH forced-command gate (whitelist commands) | beelink |

### Security model: restricted SSH

Instead of giving CI full shell access, we use an SSH forced-command gate:

```
authorized_keys entry:
  command="/path/deploy-gate.sh",no-port-forwarding,no-agent-forwarding,no-pty ssh-ed25519 <key>
```

The gate allows exactly two commands:
- `deploy agents observability` → `git pull` + `compose up`
- `receive-env agents` → reads stdin → writes `stacks/agents/.env`

Everything else is denied. Even if the deploy key leaks, the attacker can only deploy stacks or write `.env` files.

### GitHub secrets configured

| Secret | Purpose |
|--------|---------|
| `DEPLOY_SSH_KEY` | ed25519 private key for restricted SSH |
| `TS_OAUTH_CLIENT_ID` | Tailscale OAuth (joins runner to tailnet as `tag:ci`) |
| `TS_OAUTH_SECRET` | Tailscale OAuth secret |
| `BOT_APP_ID` | Agents stack: GitHub App ID |
| `BOT_APP_INSTALLATION_ID` | Agents stack: GitHub App installation ID |
| `COPILOT_GITHUB_TOKEN` | Agents stack: Copilot CLI token |
| `DISCORD_PRIVATE_WEBHOOK_URL` | Observability stack: Discord webhook |
| `CF_TUNNEL_TOKEN` | Flight-tracker stack: Cloudflare tunnel token |

## Current blocker: Tailscale SSH vs regular SSH

### The problem

1. **sshd on beelink** listens on `100.100.146.119:22` (the Tailscale IP)
2. **Tailscale SSH** intercepts port 22 on the Tailscale IP before sshd sees it
3. Tailscale SSH has its own auth — it completely **bypasses `authorized_keys`**
4. The Tailscale SSH ACL only allows `tag:desktop`:

```jsonc
"ssh": [{
  "src": ["tag:desktop"],
  "dst": ["tag:server"],
  "users": ["autogroup:nonroot", "root"],
  "action": "accept"
}]
```

5. CI runners (`tag:ci`) get rejected: `tailnet policy does not permit you to SSH to this node`

### Why the forced-command gate doesn't work (yet)

The deploy key is in `authorized_keys` on beelink, but Tailscale SSH intercepts the connection at the network layer and never passes it to sshd. The `authorized_keys` file is never consulted.

### Failed deploy attempts

- **Run 24591556717**: `~/.ssh` dir didn't exist on runner (fixed: added `mkdir -p`)
- **Run 24591580941**: `tailnet policy does not permit you to SSH to this node`

## Options to resolve

### Option A: Tailscale SSH `accept` for CI

Add `tag:ci` to the `ssh` ACL:

```jsonc
{
  "src": ["tag:ci"],
  "dst": ["tag:server"],
  "users": ["colin"],
  "action": "accept"
}
```

- ✅ Simple — no sshd changes needed
- ✅ No deploy key needed (Tailscale handles auth)
- ❌ CI gets **full shell access** as `colin` — no forced-command restriction
- ❌ deploy-gate.sh is useless (Tailscale SSH doesn't use authorized_keys)
- ❌ If Tailscale OAuth is compromised, attacker has full SSH

**If choosing this**: remove deploy key approach entirely, simplify workflow to just `ssh colin@beelink "cd /path && deploy.sh"`. The "security boundary" becomes Tailscale ACL + OAuth.

### Option B: sshd on a second port (2222) for CI

Configure sshd to also listen on port 2222. Tailscale SSH only intercepts port 22.

```
# /etc/ssh/sshd_config
Port 22        # still there for local/LAN
Port 2222      # CI deploys via deploy key
ListenAddress 100.100.146.119
```

ACL change:
```jsonc
// CI gets port 2222 (sshd) instead of 22 (Tailscale SSH)
{ "src": ["tag:ci"], "dst": ["tag:server"], "ip": ["2222", "8585"] }
```

Workflow change: `ssh -p 2222 -i deploy_key colin@beelink "deploy ..."`

- ✅ Forced-command gate works (sshd reads authorized_keys)
- ✅ Desktop Tailscale SSH unchanged
- ✅ Minimal blast radius — deploy key can only run deploy/receive-env
- ❌ Extra port to manage and document

### Option C: Disable Tailscale SSH entirely

Turn off Tailscale SSH, use regular sshd for everything.

Remove the `"ssh"` section from ACL. Desktop uses SSH keys in `authorized_keys`.

- ✅ Forced-command gate works
- ✅ Simpler mental model (one SSH path)
- ❌ Desktop loses Tailscale SSH convenience (need to manage SSH keys)
- ❌ Lose Tailscale's SSH session logging/audit

## Also broken: server .env files

During testing, `generate-env.sh` was accidentally run on the server, overwriting real secrets with "placeholder". Services are still running on cached values (Docker doesn't re-read `.env` until restart).

**Affected stacks**: agents, observability, flight-tracker  
**Fix**: Once the deploy pipeline works, a manual `workflow_dispatch` with `stack=all` will push real secrets from GitHub.  
**Until then**: Do NOT restart any containers on beelink.

## Commits so far

```
6a4a14f fix: create .ssh dir before writing deploy key
fbf6d71 feat: restricted SSH deploy with forced-command gate
552420a fix: update compose validation to use generate-env with placeholders
10b813d refactor: simplify deploy workflow with env templates and scripts
392efc0 refactor: extract deploy script, harden workflow security
```

## Tailscale ACL (current, saved in docs/private/tailscale-acl.jsonc)

Updated to grant `tag:ci` port 22 — but this hits the Tailscale SSH interception issue described above. Needs adjusting once an option is chosen.
