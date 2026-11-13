#!/usr/bin/env bash
set -euo pipefail

# Shared deployment logic lives in typed Python; this wrapper preserves the
# stable command used by manual operations and GitHub Actions.
repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
# System python3, not homelab-python3: deploy.py imports yaml, which only the host's apt python3-yaml has.
exec python3 "$repo_root/stacks/deploy/deploy.py" "$@"
