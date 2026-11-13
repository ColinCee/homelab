#!/usr/bin/env bash
set -euo pipefail

# Validate every Compose service and profile with each stack's tracked settings. Secret files need not
# exist: Compose checks them only when it starts a container. Arguments name the compose files to
# check; none means all.
files=("$@")
[[ ${#files[@]} -gt 0 ]] || files=(stacks/*/compose.yaml)
for f in "${files[@]}"; do
  echo "Validating $f..."
  settings="$(dirname "$f")/settings.env"
  if [[ -f "$settings" ]]; then
    docker compose --env-file "$settings" -f "$f" --profile '*' config --quiet
  else
    docker compose -f "$f" --profile '*' config --quiet
  fi
done
echo "All compose files valid"
