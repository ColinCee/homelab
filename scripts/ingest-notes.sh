#!/usr/bin/env bash
set -euo pipefail

NOTES_DIR="${1:-/home/colin/code/notes}"
HOMELAB_DIR="/home/colin/code/homelab"
KNOWLEDGE_APP_DIR="${HOMELAB_DIR}/stacks/knowledge/app"
KNOWLEDGE_ENV_FILE="${HOMELAB_DIR}/stacks/knowledge/.env"
AGENTS_ENV_FILE="${HOMELAB_DIR}/stacks/agents/.env"

load_env_file() {
  local env_file="$1"
  [[ -f "$env_file" ]] || return 0

  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
}

load_env_file "$KNOWLEDGE_ENV_FILE"
load_env_file "$AGENTS_ENV_FILE"

if [[ -z "${KNOWLEDGE_DB_URL:-}" ]]; then
  encoded_password=$(printf '%s' "${POSTGRES_PASSWORD:?}" | python3 -c "import sys; from urllib.parse import quote; print(quote(sys.stdin.read(), safe=''))")
  export KNOWLEDGE_DB_URL="postgresql://${POSTGRES_USER:?}:${encoded_password}@localhost:5432/${POSTGRES_DB:?}"
fi
: "${COPILOT_GITHUB_TOKEN:?COPILOT_GITHUB_TOKEN must be set}"

cd "$NOTES_DIR"
git pull --ff-only

cd "$KNOWLEDGE_APP_DIR"
uv run python -m knowledge ingest --dir "$NOTES_DIR"
