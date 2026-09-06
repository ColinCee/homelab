#!/usr/bin/env bash
set -euo pipefail

# Deploy one or more stacks. Runs on the server.
#
# Usage: scripts/deploy.sh [stack ...] (defaults to all stacks).
# Deploys this checkout without fetching or resetting Git.

cd "$(dirname "$0")/.."
if (( $# )); then
  stacks=("$@")
else
  stacks=()
  for file in stacks/*/compose.yaml; do
    stack="${file#stacks/}"
    stacks+=("${stack%/compose.yaml}")
  done
fi

compose() {
  local file="$1"
  local env_file="$2"
  shift 2

  if [[ -f "$env_file" ]]; then
    docker compose --env-file "$env_file" -f "$file" "$@"
  else
    docker compose -f "$file" "$@"
  fi
}

install_timer() {
  local unit_base="$1"
  local name
  name="$(basename "$unit_base")"
  local dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$dir"
  cp "${unit_base}.service" "${unit_base}.timer" "$dir/"
  # Runner process may lack user bus access; set it explicitly
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
  systemctl --user daemon-reload
  systemctl --user enable --now "${name}.timer"
  echo "  ⏱ ${name}.timer installed"
}

for stack in "${stacks[@]}"; do
  file="stacks/${stack}/compose.yaml"
  [[ -f "$file" ]] || { echo "❌ No compose.yaml: ${stack}" >&2; exit 1; }

  env_file="stacks/${stack}/.env"

  case "$stack" in
    knowledge)      compose "$file" "$env_file" build ingest
                    compose "$file" "$env_file" up -d --remove-orphans
                    install_timer "stacks/knowledge/knowledge-backup" ;;
    flight-tracker) compose "$file" "$env_file" pull
                    compose "$file" "$env_file" up -d --remove-orphans
                    install_timer "stacks/flight-tracker/flight-tracker-poll" ;;
    *)              compose "$file" "$env_file" up -d --remove-orphans ;;
  esac
  echo "✅ ${stack}"
done
