#!/usr/bin/env bash
set -euo pipefail

# Shared deployment logic lives in typed Python; this wrapper preserves the
# stable command used by manual operations and GitHub Actions.
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$repo_root/scripts/deploy.py" "$@"
