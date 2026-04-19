#!/usr/bin/env bash
# Check version consistency between mise.toml and Dockerfiles.
# Fails CI if python or uv versions drift between these sources of truth.
set -euo pipefail

AGENTS_DOCKERFILE="stacks/agents/app/Dockerfile"
KNOWLEDGE_DOCKERFILE="stacks/knowledge/app/Dockerfile"
MISE="mise.toml"

errors=0

check_python_from() {
  local label="$1" dockerfile="$2"
  local from_version
  from_version=$(head -5 "$dockerfile" | grep '^FROM python:' | sed 's/FROM python:\([0-9.]*\).*/\1/')
  if [ "$mise_python" != "$from_version" ]; then
    echo "❌ Python drift: mise.toml=$mise_python, $label FROM=$from_version"
    errors=$((errors + 1))
  fi
}

check_uv_copy() {
  local label="$1" dockerfile="$2"
  local uv_version
  uv_version=$(grep 'COPY --from=ghcr.io/astral-sh/uv:' "$dockerfile" | sed 's/.*uv:\([0-9.]*\).*/\1/')
  if [ "$mise_uv" != "$uv_version" ]; then
    echo "❌ uv drift: mise.toml=$mise_uv, $label COPY=$uv_version"
    errors=$((errors + 1))
  fi
}

# ── Versions from mise.toml ────────────────────────
mise_python=$(grep '^python' "$MISE" | sed 's/.*"\(.*\)"/\1/')
mise_uv=$(grep '^uv' "$MISE" | sed 's/.*"\(.*\)"/\1/')

# ── Agents Dockerfile ──────────────────────────────
check_python_from "agents Dockerfile" "$AGENTS_DOCKERFILE"
check_uv_copy "agents Dockerfile" "$AGENTS_DOCKERFILE"

# Agents Dockerfile also has an ARG for the mise registration step
dockerfile_arg=$(grep '^ARG PYTHON_VERSION=' "$AGENTS_DOCKERFILE" | sed 's/ARG PYTHON_VERSION=//')
if [ "$mise_python" != "$dockerfile_arg" ]; then
  echo "❌ Python drift: mise.toml=$mise_python, agents Dockerfile ARG=$dockerfile_arg"
  errors=$((errors + 1))
fi

# ── Knowledge Dockerfile ───────────────────────────
check_python_from "knowledge Dockerfile" "$KNOWLEDGE_DOCKERFILE"
check_uv_copy "knowledge Dockerfile" "$KNOWLEDGE_DOCKERFILE"

# ── Summary ────────────────────────────────────────
if [ "$errors" -gt 0 ]; then
  echo ""
  echo "Found $errors version inconsistencies. Fix them before merging."
  exit 1
fi

echo "✅ All versions consistent"
