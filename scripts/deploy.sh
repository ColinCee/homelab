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

  case "$stack" in
    agents)         docker compose -f "$file" up -d --build --remove-orphans ;;
    observability)  docker compose -f "$file" up -d --remove-orphans
                    scripts/sync-dashboards.sh ;;
    flight-tracker) docker compose -f "$file" pull
                    docker compose -f "$file" up -d --remove-orphans
                    install_timer "stacks/flight-tracker/flight-tracker-poll" ;;
    *)              docker compose -f "$file" up -d --remove-orphans ;;
  esac
  echo "✅ ${stack}"
done
