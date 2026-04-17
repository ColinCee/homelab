#!/usr/bin/env bash
set -euo pipefail

STACK=${1:?Usage: deploy.sh <stack-name>}
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/stacks/${STACK}/compose.yaml"

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "❌ No compose.yaml found for stack: ${STACK}" >&2
  exit 1
fi

cd "$REPO_ROOT"
git pull --ff-only

case "$STACK" in
  agents)
    docker compose -f "$COMPOSE_FILE" up -d --build --remove-orphans
    ;;
  flight-tracker)
    docker compose -f "$COMPOSE_FILE" pull
    docker compose -f "$COMPOSE_FILE" up -d --remove-orphans
    ;;
  *)
    docker compose -f "$COMPOSE_FILE" up -d --remove-orphans
    ;;
esac

echo "✅ ${STACK} deployed"
