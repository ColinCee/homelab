#!/usr/bin/env bash
set -euo pipefail

# Validate all compose files with placeholder secrets.
# Exports a placeholder for every ${VAR} found in .env.example files
# so that required-variable checks (${VAR:?}) pass during validation.

# Export placeholders for all secret references in .env.example files.
# grep -oE used instead of -P for portability (no PCRE dependency).
mapfile -t env_vars < <(
  grep -ohE '\$\{[A-Z_][A-Z0-9_]*\}' stacks/*/.env.example 2>/dev/null \
    | sed -E 's/^\$\{([^}]+)\}$/\1/' \
    | sort -u || true
)
for var in "${env_vars[@]}"; do
  export "${var}=placeholder"
done

# Generate .env files from templates
stacks=()
for f in stacks/*/compose.yaml; do
  stacks+=("$(basename "$(dirname "$f")")")
done
scripts/generate-env.sh "${stacks[@]}"

# Validate each compose file
for f in stacks/*/compose.yaml; do
  echo "Validating $f..."
  env_file="$(dirname "$f")/.env"
  if [[ -f "$env_file" ]]; then
    docker compose --env-file "$env_file" -f "$f" config --quiet || exit 1
  else
    docker compose -f "$f" config --quiet || exit 1
  fi
done
echo "All compose files valid"
