#!/usr/bin/env bash
set -euo pipefail

# SSH forced-command gate for CI deploys.
# Installed in authorized_keys with:
#   command="/home/colin/code/homelab/scripts/deploy-gate.sh",no-port-forwarding,no-agent-forwarding,no-pty ssh-ed25519 ...
#
# Allows only two operations:
#   1. deploy <stack...>     — pull latest code and restart stacks
#   2. receive-env <stack>   — accept .env content on stdin for a stack

REPO="/home/colin/code/homelab"
CMD="${SSH_ORIGINAL_COMMAND:-}"

case "$CMD" in
  "deploy "*)
    stacks="${CMD#deploy }"
    cd "$REPO"
    git pull --ff-only
    export STACKS="$stacks"
    exec scripts/deploy.sh
    ;;
  "receive-env "*)
    stack="${CMD#receive-env }"
    target="${REPO}/stacks/${stack}/.env"
    # Validate stack name (alphanumeric + hyphens only)
    if [[ ! "$stack" =~ ^[a-z0-9-]+$ ]]; then
      echo "Invalid stack name: $stack" >&2
      exit 1
    fi
    if [[ ! -d "${REPO}/stacks/${stack}" ]]; then
      echo "Unknown stack: $stack" >&2
      exit 1
    fi
    cat > "$target"
    echo "✅ .env written for ${stack}"
    ;;
  *)
    echo "Denied: ${CMD:-<empty>}" >&2
    echo "Allowed commands: deploy <stacks>, receive-env <stack>" >&2
    exit 1
    ;;
esac
