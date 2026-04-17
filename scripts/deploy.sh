#!/usr/bin/env bash
set -euo pipefail

# Deploy one or more stacks. Runs on the server.
#
# Usage: deploy.sh agents observability
#    or: STACKS="agents observability" deploy.sh

if (( $# )); then
  stacks=("$@")
else
  read -ra stacks <<< "${STACKS:?Set STACKS env var or pass stack names as args}"
fi

cd "$(dirname "$0")/.."
git pull --ff-only

for stack in "${stacks[@]}"; do
  file="stacks/${stack}/compose.yaml"
  [[ -f "$file" ]] || { echo "❌ No compose.yaml: ${stack}" >&2; exit 1; }

  case "$stack" in
    agents)         docker compose -f "$file" up -d --build --remove-orphans ;;
    flight-tracker) docker compose -f "$file" pull
                    docker compose -f "$file" up -d --remove-orphans ;;
    *)              docker compose -f "$file" up -d --remove-orphans ;;
  esac
  echo "✅ ${stack}"
done
