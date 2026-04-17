#!/usr/bin/env bash
set -euo pipefail

# Detect which stacks changed and need deployment.
# Set INPUT_STACK to override auto-detection (e.g. "agents" or "all").
# Writes "stacks=..." to $GITHUB_OUTPUT (or stdout if unset).

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT="${GITHUB_OUTPUT:-/dev/stdout}"

# Auto-discover all stacks from stacks/*/compose.yaml
all=()
for f in "$REPO_ROOT"/stacks/*/compose.yaml; do
  all+=("$(basename "$(dirname "$f")")")
done

if [[ "${INPUT_STACK:-}" == "all" ]]; then
  echo "stacks=${all[*]}" >> "$OUTPUT"
  echo "Deploying: all (${all[*]})"
  exit 0
elif [[ -n "${INPUT_STACK:-}" ]]; then
  echo "stacks=${INPUT_STACK}" >> "$OUTPUT"
  echo "Deploying: ${INPUT_STACK}"
  exit 0
fi

changed=$(git diff --name-only HEAD~1 HEAD)
stacks=""

# Agents also deploys on Python dependency changes
if echo "$changed" | grep -qE '^(stacks/agents/|pyproject\.toml|uv\.lock)'; then
  stacks+="agents "
fi
for s in "${all[@]}"; do
  [[ "$s" == "agents" ]] && continue  # already handled above
  if echo "$changed" | grep -q "^stacks/$s/"; then stacks+="$s "; fi
done
stacks="${stacks% }"

echo "stacks=${stacks}" >> "$OUTPUT"
echo "Deploying: ${stacks:-nothing}"
