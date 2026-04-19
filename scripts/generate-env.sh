#!/usr/bin/env bash
set -euo pipefail

# Generate .env files from .env.example templates.
# Variables like ${SECRET_NAME} are replaced from the environment;
# constants (no ${}) pass through unchanged.
#
# Usage: generate-env.sh agents observability
#    or: STACKS="agents observability" generate-env.sh
#
# If ALL_SECRETS is set (JSON from GitHub Actions toJson(secrets)),
# each key is exported so envsubst can substitute them.

if [[ -n "${ALL_SECRETS:-}" ]]; then
  eval "$(echo "$ALL_SECRETS" | jq -r 'to_entries[] | select(.value != "") | "export \(.key)=\(.value | @sh)"')"
fi

if (( $# )); then
  stacks=("$@")
else
  read -ra stacks <<< "${STACKS:?Set STACKS env var or pass stack names as args}"
fi

for stack in "${stacks[@]}"; do
  example="stacks/${stack}/.env.example"
  [[ -f "$example" ]] || continue
  vars=$(grep -oP '\$\{[A-Z_]+\}' "$example" | sort -u | tr '\n' ' ')
  # Single-quote values so docker compose doesn't interpolate $ in passwords
  envsubst "$vars" < "$example" | sed "s/=\(.*\)/='\1'/" > "stacks/${stack}/.env"
done
