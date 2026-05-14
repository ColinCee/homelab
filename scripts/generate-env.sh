#!/usr/bin/env bash
set -euo pipefail

# Generate .env files from .env.example templates.
# Variables like ${SECRET_NAME} are replaced from the environment;
# constants (no ${}) pass through unchanged.
# Generated .env files are data for Docker Compose; never source them as shell.
#
# Usage: generate-env.sh agents observability
#    or: STACKS="agents observability" generate-env.sh

quote_env_value() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\'/\\\'}"
  printf "'%s'" "$value"
}

required_vars_for() {
  local example="$1"
  grep -ohE '\$\{[A-Z_][A-Z0-9_]*\}' "$example" 2>/dev/null \
    | sed -E 's/^\$\{([^}]+)\}$/\1/' \
    | sort -u || true
}

ensure_required_vars() {
  local stack="$1"
  shift
  local missing=()
  local var
  for var in "$@"; do
    if [[ -z "${!var:-}" ]]; then
      missing+=("$var")
    fi
  done

  if (( ${#missing[@]} )); then
    echo "Missing required environment variable(s) for ${stack}: ${missing[*]}" >&2
    exit 1
  fi
}

render_env_file() {
  local stack="$1"
  local example="$2"
  local output="$3"
  shift 3
  local vars=("$@")
  local envsubst_vars=""
  local var
  local tmp

  for var in "${vars[@]}"; do
    envsubst_vars+="\${${var}} "
  done

  tmp="$(mktemp "${output}.XXXXXX")"
  envsubst "$envsubst_vars" < "$example" | while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ -z "$line" || "$line" == \#* || "$line" != *=* ]]; then
      printf '%s\n' "$line"
      continue
    fi

    printf '%s=%s\n' "${line%%=*}" "$(quote_env_value "${line#*=}")"
  done > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$output"
  echo "Generated ${output} for ${stack}"
}

if (( $# )); then
  stacks=("$@")
else
  read -ra stacks <<< "${STACKS:?Set STACKS env var or pass stack names as args}"
fi

for stack in "${stacks[@]}"; do
  example="stacks/${stack}/.env.example"
  [[ -f "$example" ]] || continue
  mapfile -t vars < <(required_vars_for "$example")
  ensure_required_vars "$stack" "${vars[@]}"
  render_env_file "$stack" "$example" "stacks/${stack}/.env" "${vars[@]}"
done
