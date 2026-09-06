#!/usr/bin/env bash
set -euo pipefail

# Validate every Compose service and profile with placeholder secrets, without
# overwriting .env.
mapfile -t env_vars < <(
  for compose_file in stacks/*/compose.yaml; do
    example="${compose_file%/compose.yaml}/.env.example"
    [[ -f "$example" ]] || continue
    grep -ohE '\$\{[A-Z_][A-Z0-9_]*\}' "$example"
  done \
    | sed -E 's/^\$\{([^}]+)\}$/\1/' \
    | sort -u || true
)
for var in "${env_vars[@]}"; do
  export "${var}=placeholder"
done

for f in stacks/*/compose.yaml; do
  echo "Validating $f..."
  env_file="$(dirname "$f")/.env.example"
  if [[ -f "$env_file" ]]; then
    docker compose --env-file "$env_file" -f "$f" --profile '*' config --quiet
  else
    docker compose -f "$f" --profile '*' config --quiet
  fi
done
echo "All compose files valid"
