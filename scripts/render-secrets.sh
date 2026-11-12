#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
# System python3, not homelab-python3: render_secrets.py imports yaml, which only the host's apt python3-yaml has.
exec python3 "$repo_root/scripts/render_secrets.py" "$@"
