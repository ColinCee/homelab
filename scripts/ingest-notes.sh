#!/usr/bin/env bash
set -euo pipefail

# Compatibility entrypoint for the notes workflow. Keep this path stable while
# the stack-owned implementation lives beside its Compose file.
if (( $# > 1 )); then
  echo "Usage: scripts/ingest-notes.sh [notes-directory]" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
notes_dir="${1:-/home/colin/code/notes}"
exec python3 "$repo_root/stacks/knowledge/operations.py" ingest --notes-dir "$notes_dir"
