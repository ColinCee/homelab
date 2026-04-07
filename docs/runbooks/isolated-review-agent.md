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

## 3. Create Fine-Grained PAT

1. Go to **Settings → Developer Settings → Personal Access Tokens → Fine-grained tokens**
2. Create token with:
   - **Name:** `homelab-copilot-cli`
   - **Resource owner:** `ColinCee`
   - **Repository access:** `ColinCee/homelab`
   - **Permissions:** `Copilot Requests: Read`
   - **Expiration:** 1 year (rotate before expiry)

## 4. Deploy Secrets to Beelink

```bash
# Copy the private key
scp ~/Downloads/homelab-review-bot.*.private-key.pem \
  beelink:/home/colin/secrets/github-app.pem

# Set environment variables (add to ~/.bashrc or systemd env file)
ssh beelink
cat >> ~/.env.agents <<'EOF'
GITHUB_TOKEN=github_pat_...         # Fine-grained PAT from step 3
GITHUB_APP_ID=123456                # App ID from step 1
GITHUB_APP_INSTALLATION_ID=78901   # Installation ID from step 2
GITHUB_APP_KEY_FILE=/home/colin/secrets/github-app.pem
EOF
```

## 5. Deploy the Stack

```bash
# Source the env vars and deploy
set -a && source ~/.env.agents && set +a
mise run deploy:agents
```

## 6. Update Branch Protection

1. Go to **Settings → Branches → main → Edit**
2. Under **Require approvals**, add `homelab-review-bot` as a required reviewer
3. This ensures the bot's APPROVE satisfies the review requirement

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
