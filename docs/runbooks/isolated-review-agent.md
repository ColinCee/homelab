# Setting Up the Isolated Review Agent

Manual steps to activate the isolated review agent ([ADR-004](../decisions/004-isolated-review-agent.md)).

## 1. Create GitHub App

1. Go to **Settings → Developer Settings → GitHub Apps → New GitHub App**
2. Configure:
   - **Name:** `homelab-review-bot`
   - **Homepage URL:** `https://github.com/ColinCee/homelab`
   - **Webhook:** Deactivate (we don't need it)
   - **Permissions:**
     - Pull Requests: **Read & Write**
     - Contents: **Read-only**
   - **Where can this GitHub App be installed?** Only on this account
3. Click **Create GitHub App**
4. Note the **App ID** from the app's general settings page
5. Generate a **Private Key** — downloads a `.pem` file

## 2. Install the App

1. On the App page, click **Install App**
2. Select **Only select repositories** → `ColinCee/homelab`
3. Note the **Installation ID** from the URL (`/installations/<ID>`)

## 3. Create Fine-Grained PAT for Copilot CLI

1. Go to **Settings → Developer Settings → Personal Access Tokens → Fine-grained tokens**
   https://github.com/settings/personal-access-tokens/new
2. Configure:
   - **Name:** `homelab-copilot-cli`
   - **Expiration:** 1 year (rotate before expiry)
   - **Resource owner:** `ColinCee`
   - **Repository access:** Public repositories (read-only)
   - **Account permissions → Copilot Requests:** Read-only
3. Click **Generate token** and copy the value

This token only grants Copilot LLM API access — it cannot modify repos or access GitHub APIs.

## 4. Deploy Secrets to Beelink

```bash
# Copy the private key
scp ~/Downloads/homelab-review-bot.*.private-key.pem \
  beelink:/home/colin/secrets/github-app.pem

# Create the secrets env file (referenced by compose.yaml via absolute path)
ssh beelink
cat > ~/secrets/agents.env <<'EOF'
GITHUB_APP_ID=<from app settings page>
GITHUB_APP_INSTALLATION_ID=<from installation URL>
COPILOT_GITHUB_TOKEN=<fine-grained PAT>
EOF
chmod 600 ~/secrets/agents.env
```

## 5. Deploy the Stack

```bash
mise run deploy:agents
```

## 6. Branch Protection

GitHub App bot approvals **do not count** toward required review counts (platform limitation). The repo uses a ruleset with:

- Required approvals: **0** (bot review is advisory)
- Required status check: `check` (CI gates merges)
- Dismiss stale reviews on push: enabled

The workflow is: bot posts review → you read it, fix blockers → self-approve and merge.

## 7. Verify

```bash
# Check agent is running
curl -sf http://beelink:8585/health

# Test with a real PR (create a test PR first)
curl -X POST http://beelink:8585/review \
  -H "Content-Type: application/json" \
  -d '{"repo": "ColinCee/homelab", "pr_number": <PR_NUMBER>}'
# Should return 202

# Check review status
curl http://beelink:8585/review/<PR_NUMBER>
```
