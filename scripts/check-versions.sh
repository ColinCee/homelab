#!/usr/bin/env bash
# Check version consistency across mise.toml, Dockerfile, and pyproject.toml.
# Fails CI if versions drift between these files.
set -euo pipefail

DOCKERFILE="stacks/agents/app/Dockerfile"
MISE="mise.toml"

errors=0

# ── Python ──────────────────────────────────────────
mise_python=$(grep '^python' "$MISE" | sed 's/.*"\(.*\)"/\1/')
dockerfile_from=$(head -5 "$DOCKERFILE" | grep '^FROM python:' | sed 's/FROM python:\([0-9.]*\).*/\1/')
dockerfile_arg=$(grep '^ARG PYTHON_VERSION=' "$DOCKERFILE" | sed 's/ARG PYTHON_VERSION=//')

if [ "$mise_python" != "$dockerfile_from" ]; then
  echo "❌ Python drift: mise.toml=$mise_python, Dockerfile FROM=$dockerfile_from"
  errors=$((errors + 1))
fi
if [ "$mise_python" != "$dockerfile_arg" ]; then
  echo "❌ Python drift: mise.toml=$mise_python, Dockerfile ARG=$dockerfile_arg"
  errors=$((errors + 1))
fi

# ── uv ──────────────────────────────────────────────
mise_uv=$(grep '^uv' "$MISE" | sed 's/.*"\(.*\)"/\1/')
dockerfile_uv=$(grep 'COPY --from=ghcr.io/astral-sh/uv:' "$DOCKERFILE" | sed 's/.*uv:\([0-9.]*\).*/\1/')

if [ "$mise_uv" != "$dockerfile_uv" ]; then
  echo "❌ uv drift: mise.toml=$mise_uv, Dockerfile COPY=$dockerfile_uv"
  errors=$((errors + 1))
fi

# ── Summary ─────────────────────────────────────────
if [ "$errors" -gt 0 ]; then
  echo ""
  echo "Found $errors version inconsistencies. Fix them before merging."
  exit 1
fi

echo "✅ All versions consistent"
