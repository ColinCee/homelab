#!/usr/bin/env bash
set -euo pipefail

# Sync the server checkout to the commit being deployed.
#
# GitHub Actions passes DEPLOY_REF (github.sha) and DEPLOY_GIT_REF (github.ref)
# so workflow_dispatch runs from non-main refs can fetch the commit before reset.
# Local/server manual runs without DEPLOY_REF keep the historical origin/main behavior.

cd "$(dirname "$0")/.."

if [[ -n "${DEPLOY_REF:-}" ]]; then
  git fetch --no-tags origin "${DEPLOY_GIT_REF:-refs/heads/main}"
  git reset --hard "$DEPLOY_REF"
else
  git fetch origin main
  git reset --hard origin/main
fi
