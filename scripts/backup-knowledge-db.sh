#!/usr/bin/env bash
set -euo pipefail

# Dump the knowledge Postgres database to a host-side backup directory.
# Intended for the user-level systemd timer installed by scripts/deploy.sh.

backup_dir="${1:-${KNOWLEDGE_BACKUP_DIR:-/home/colin/backups/knowledge}}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="${repo_root}/stacks/knowledge/compose.yaml"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_file="${backup_dir}/knowledge-${timestamp}.dump"
tmp_file="${backup_file}.tmp"

mkdir -p "$backup_dir"
trap 'rm -f "$tmp_file"' EXIT

cd "$repo_root"
docker compose -f "$compose_file" exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-acl' \
  > "$tmp_file"

docker compose -f "$compose_file" exec -T postgres pg_restore --list < "$tmp_file" >/dev/null
mv "$tmp_file" "$backup_file"
trap - EXIT
find "$backup_dir" -type f -name 'knowledge-*.dump' -mtime +14 -delete

echo "Wrote ${backup_file}"
