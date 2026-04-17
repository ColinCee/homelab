#!/usr/bin/env bash
set -euo pipefail

# Generate .env files from .env.example templates.
# Variables like ${SECRET_NAME} are replaced from the environment;
# constants (no ${}) pass through unchanged.
#
# Usage: generate-env.sh agents observability
#    or: STACKS="agents observability" generate-env.sh

if (( $# )); then
  stacks=("$@")
else
  read -ra stacks <<< "${STACKS:?Set STACKS env var or pass stack names as args}"
fi

for stack in "${stacks[@]}"; do
  example="stacks/${stack}/.env.example"
  [[ -f "$example" ]] || continue
  vars=$(grep -oP '\$\{[A-Z_]+\}' "$example" | sort -u | tr '\n' ' ')
  envsubst "$vars" < "$example" > "stacks/${stack}/.env"
done
