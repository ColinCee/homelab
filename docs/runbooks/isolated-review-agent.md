# Operating the Agent Stack

Setup, verification, and day-2 operations for the agent service
([ADR-010](../decisions/010-agent-security-model.md),
[ADR-008](../decisions/008-documentation-ownership.md)).

## Source-Owned Operational Contracts

Use the runbook for procedure. Use source for exact values that drift easily.

| Fact | Authoritative source |
|------|----------------------|
| HTTP request/response models and status payloads | `stacks/agents/app/main.py` |
| Review orchestrator and linked-issue parsing | `stacks/agents/app/review/orchestrator.py` |
| Implement orchestrator and trust validation | `stacks/agents/app/implement/orchestrator.py` |
| GitHub API helpers and bot identity | `stacks/agents/app/services/github.py` |
| CLI timeout and stats parsing | `stacks/agents/app/services/copilot.py` |
| Worktree lifecycle and cleanup markers | `stacks/agents/app/services/git.py` |
| Implement/review lifecycle contract | `.github/skills/bot-implement/SKILL.md`, `.github/skills/bot-review/SKILL.md` |
| Required deploy-time env vars | `stacks/agents/compose.yaml` and `stacks/agents/.env.example` |

## 1. Create the GitHub App

1. Go to **Settings → Developer Settings → GitHub Apps → New GitHub App**.
2. Configure:
   - **Name:** match the bot identity expected by `services/github.py`
     (currently `colins-homelab-bot`)
   - **Homepage URL:** `https://github.com/ColinCee/homelab`
   - **Webhook:** disabled
   - **Permissions:**
     - Pull Requests: **Read & Write**
     - Contents: **Read & Write**
     - Issues: **Read & Write**
3. Create the app, note the **App ID**, and generate a **private key**.
4. Install the app only on `ColinCee/homelab` and note the **Installation ID**.

`Contents: Read & Write` is required for the implementation lifecycle: the
CLI commits and pushes to bot-owned `agent/issue-*` branches before
opening or updating the PR.

## 2. Create the Copilot token

Create a fine-grained personal access token for Copilot CLI:

1. Go to <https://github.com/settings/personal-access-tokens/new>.
2. Configure:
   - **Name:** `homelab-copilot-cli`
   - **Resource owner:** `ColinCee`
   - **Repository access:** public repositories
   - **Account permissions → Copilot Requests:** **Read-only**
3. Copy the token value.

This token is only for Copilot inference. It is separate from the `GH_TOKEN`
(GitHub App installation token) that gives the CLI repo access.

## 3. Configure Dokploy secrets

Copy the private key to the Beelink:

```bash
scp ~/Downloads/colins-homelab-bot.*.private-key.pem \
  beelink:/home/colin/secrets/github-app.pem
```

Set the variables required by `stacks/agents/compose.yaml` in the Dokploy UI for
the agent stack. The current operator-facing set is:

| Variable | Value |
|----------|-------|
| `GITHUB_APP_ID` | GitHub App ID |
| `GITHUB_APP_INSTALLATION_ID` | GitHub App installation ID |
| `GITHUB_APP_KEY_FILE` | Host path to the PEM file (for example `/home/colin/secrets/github-app.pem`) |
| `COPILOT_GITHUB_TOKEN` | Fine-grained PAT with `Copilot Requests: Read` |

Dokploy writes these to `.env` next to the compose file, which is why CI can
validate the compose using `stacks/agents/.env.example`.

## 4. Deploy

Dokploy auto-deploys from `main`. For a manual deploy:

```bash
mise run deploy:agents
```

## 5. Trigger workflows

### Review a pull request

1. Comment `/review` on the PR.
2. GitHub Actions connects over Tailscale and POSTs to `/review`.
3. The agent returns `202 Accepted` immediately and works in the background.

### Implement an issue

1. Add the `agent` label to the issue or comment `/implement`.
2. The agent creates `agent/issue-<N>`, and the CLI handles the full lifecycle:
   implement → self-review → fix (up to 2 rounds) → mark ready → merge.

## 6. Verify

Smoke-test the API directly if needed:

```bash
# Health
curl -sf http://beelink:8585/health

# Dispatch a review
curl -sf -X POST http://beelink:8585/review \
  -H "Content-Type: application/json" \
  -d '{"repo":"ColinCee/homelab","pr_number":123}'

# Check review status
curl -sf 'http://beelink:8585/review/123?repo=ColinCee/homelab'

# Dispatch an implementation
curl -sf -X POST http://beelink:8585/implement \
  -H "Content-Type: application/json" \
  -d '{"repo":"ColinCee/homelab","issue_number":52}'

# Check implementation status
curl -sf 'http://beelink:8585/implement/52?repo=ColinCee/homelab'
```

## 7. Debugging

SSH into the Beelink from any machine on the tailnet:

```bash
ssh beelink
```

View live agent logs (the container name is managed by Dokploy and may change):

```bash
# Find the agent container
docker ps --filter "name=agent" --format '{{.Names}}'

# Tail recent logs
docker logs <container-name> --since 1h -f

# Filter for orchestrator events
docker logs <container-name> --since 1h 2>&1 \
  | grep -E "(Implementing|Starting review|Review complete|WARNING|ERROR)"
```

Each Copilot CLI invocation logs a `Requests  N Premium` line showing cost, and
a `Session transcript captured` line showing the full agent reasoning.

## 8. Operational Gotchas

- **`/review` is manual-only.** The workflow does not auto-review on PR open,
  synchronize, or ready-for-review.
- **Self-review is informational.** When the bot reviews its own PR, GitHub
  forces the review to be a `COMMENT`, so thread-resolution behavior differs
  from a normal human review.
- **Agent branches are disposable state.** Reruns can reuse the same
  `agent/issue-*` branch name; pushes are force-updated intentionally.
- **Worktree cleanup is deferred.** Crash-orphaned worktrees can linger until
  the retention window expires because cleanup depends on marker files and the
  periodic reaper, not only on graceful teardown.
