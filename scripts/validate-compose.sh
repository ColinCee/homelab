#!/usr/bin/env bash
set -euo pipefail

# Validate all compose files with placeholder secrets.
# Exports a placeholder for every ${VAR} found in .env.example files
# so that required-variable checks (${VAR:?}) pass during validation.

# Export placeholders for all secret references in .env.example files.
# grep -oE used instead of -P for portability (no PCRE dependency).
for var in $(grep -ohE '\$\{[A-Z_]+' stacks/*/.env.example 2>/dev/null | sed 's/\${//' | sort -u); do
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
  docker compose -f "$f" config --quiet || exit 1
done
echo "All compose files valid"
