#!/usr/bin/env bash
set -euo pipefail

# Validate all compose files with placeholder secrets.
# Exports a placeholder for every ${VAR} found in .env.example files
# so that required-variable checks (${VAR:?}) pass during validation.

while IFS= read -r var; do
  export "$var=placeholder"
done < <(grep -ohP '\$\{\K[A-Z_]+' stacks/*/.env.example 2>/dev/null | sort -u)

read -ra stack_names < <(find stacks/*/compose.yaml -printf '%h\n' | xargs -n1 basename | tr '\n' ' ')
scripts/generate-env.sh "${stack_names[@]}"

for f in stacks/*/compose.yaml; do
  echo "Validating $f..."
  docker compose -f "$f" config --quiet || exit 1
done
echo "All compose files valid"
