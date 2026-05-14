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
if [[ "${SKIP_DEPLOY_CHECKOUT_SYNC:-}" != "1" ]]; then
  scripts/prepare-deploy-checkout.sh
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

read_generated_env_value() {
  local env_file="$1"
  local key="$2"
  local line
  local value

  [[ -f "$env_file" ]] || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == "${key}="* ]] || continue
    value="${line#*=}"
    if [[ "$value" == \'*\' && "$value" == *\' ]]; then
      value="${value:1:${#value}-2}"
      value="${value//\\\'/$'\''}"
      value="${value//\\\\/\\}"
    fi
    printf '%s' "$value"
    return 0
  done < "$env_file"
  return 1
}

ensure_grafana_password() {
  local env_file="$1"

  if [[ -n "${GRAFANA_ADMIN_PASSWORD:-}" ]]; then
    return
  fi
  if GRAFANA_ADMIN_PASSWORD="$(read_generated_env_value "$env_file" GRAFANA_ADMIN_PASSWORD)"; then
    export GRAFANA_ADMIN_PASSWORD
    return
  fi
  echo "❌ GRAFANA_ADMIN_PASSWORD is required for observability deploy" >&2
  exit 1
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
    agents)         compose "$file" "$env_file" up -d --build --remove-orphans ;;
    knowledge)      compose "$file" "$env_file" build ingest
                    compose "$file" "$env_file" up -d --remove-orphans
                    install_timer "stacks/knowledge/knowledge-backup" ;;
    observability)  compose "$file" "$env_file" up -d --remove-orphans
                    ensure_grafana_password "$env_file"
                    scripts/sync-dashboards.sh ;;
    flight-tracker) compose "$file" "$env_file" pull
                    compose "$file" "$env_file" up -d --remove-orphans
                    install_timer "stacks/flight-tracker/flight-tracker-poll" ;;
    *)              compose "$file" "$env_file" up -d --remove-orphans ;;
  esac
  echo "✅ ${stack}"
done
