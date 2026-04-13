#!/usr/bin/env bash
# Check Python version consistency across Dockerfile, mise.toml, and pyproject.toml.
# Runs as part of `mise run ci` to catch drift before it reaches production.
set -euo pipefail

DOCKERFILE="stacks/agents/app/Dockerfile"
MISE_TOML="mise.toml"
ROOT_PYPROJECT="pyproject.toml"
AGENT_PYPROJECT="stacks/agents/app/pyproject.toml"

errors=0

# Extract Python major.minor.patch from Dockerfile base image (e.g. "3.14.4" from "python:3.14.4-slim")
dockerfile_python=$(grep -oP '^FROM python:\K[0-9]+\.[0-9]+\.[0-9]+' "$DOCKERFILE") || {
    echo "❌ Could not extract Python version from $DOCKERFILE (expected pinned patch version, e.g. python:3.14.4-slim)"
    errors=$((errors + 1))
}

# Extract Python version from mise.toml (e.g. "3.14.4")
mise_python=$(grep -oP '^python\s*=\s*"\K[0-9]+\.[0-9]+\.[0-9]+' "$MISE_TOML") || {
    echo "❌ Could not extract Python version from $MISE_TOML"
    errors=$((errors + 1))
}

# Extract major.minor from pyproject.toml requires-python (e.g. "3.14" from ">=3.14")
root_requires=$(grep -oP 'requires-python\s*=\s*">=\K[0-9]+\.[0-9]+' "$ROOT_PYPROJECT") || {
    echo "❌ Could not extract requires-python from $ROOT_PYPROJECT"
    errors=$((errors + 1))
}

agent_requires=$(grep -oP 'requires-python\s*=\s*">=\K[0-9]+\.[0-9]+' "$AGENT_PYPROJECT") || {
    echo "❌ Could not extract requires-python from $AGENT_PYPROJECT"
    errors=$((errors + 1))
}

# Derive major.minor from the pinned versions
dockerfile_minor=${dockerfile_python%.*}

# Check Dockerfile and mise.toml agree on patch version
if [[ -n "${dockerfile_python:-}" && -n "${mise_python:-}" ]]; then
    if [[ "$dockerfile_python" != "$mise_python" ]]; then
        echo "❌ Python version mismatch: $DOCKERFILE has $dockerfile_python, $MISE_TOML has $mise_python"
        errors=$((errors + 1))
    fi
fi

# Check pyproject.toml floors match the runtime major.minor
if [[ -n "${dockerfile_minor:-}" && -n "${root_requires:-}" ]]; then
    if [[ "$root_requires" != "$dockerfile_minor" ]]; then
        echo "❌ requires-python mismatch: $ROOT_PYPROJECT has >=$root_requires, runtime is $dockerfile_minor"
        errors=$((errors + 1))
    fi
fi

if [[ -n "${root_requires:-}" && -n "${agent_requires:-}" ]]; then
    if [[ "$root_requires" != "$agent_requires" ]]; then
        echo "❌ requires-python mismatch: $ROOT_PYPROJECT has >=$root_requires, $AGENT_PYPROJECT has >=$agent_requires"
        errors=$((errors + 1))
    fi
fi

if [[ "$errors" -gt 0 ]]; then
    echo ""
    echo "Found $errors version consistency error(s). Fix the versions above to match."
    exit 1
fi

echo "Python version consistency check passed ✅ (${dockerfile_python:-?})"
