#!/usr/bin/env bash
set -euo pipefail

# Push dashboard JSON files to Grafana via HTTP API.
# Idempotent — safe to run repeatedly. Dashboards are matched by UID.
#
# Requires: curl, python3 (for JSON envelope construction)
# Environment: GRAFANA_ADMIN_PASSWORD (required)
#
# Usage: sync-dashboards.sh
#    or: GRAFANA_URL=http://host:3001 sync-dashboards.sh

GRAFANA_URL="${GRAFANA_URL:-http://100.100.146.119:3001}"
GRAFANA_AUTH="admin:${GRAFANA_ADMIN_PASSWORD:?Set GRAFANA_ADMIN_PASSWORD}"
DASHBOARD_DIR="$(dirname "$0")/../stacks/observability/dashboards"
FOLDER_UID="homelab"
FOLDER_TITLE="Homelab"

# Wait for Grafana to be ready (up to 30s)
echo "⏳ Waiting for Grafana at ${GRAFANA_URL}..."
for i in $(seq 1 30); do
  if curl -sf "${GRAFANA_URL}/api/health" > /dev/null 2>&1; then
    echo "✅ Grafana is ready"
    break
  fi
  if (( i == 30 )); then
    echo "❌ Grafana not ready after 30s" >&2
    exit 1
  fi
  sleep 1
done

# Ensure target folder exists (409/412 = already exists — that's fine)
status=$(curl -s -o /dev/null -w '%{http_code}' -u "${GRAFANA_AUTH}" \
  -X POST "${GRAFANA_URL}/api/folders" \
  -H "Content-Type: application/json" \
  -d "{\"uid\": \"${FOLDER_UID}\", \"title\": \"${FOLDER_TITLE}\"}")

if [[ "$status" == "200" ]]; then
  echo "📁 Created folder '${FOLDER_TITLE}'"
elif [[ "$status" == "409" || "$status" == "412" ]]; then
  echo "📁 Folder '${FOLDER_TITLE}' already exists"
else
  echo "❌ Failed to create folder (HTTP ${status})" >&2
  exit 1
fi

# Push each dashboard
errors=0
for f in "${DASHBOARD_DIR}"/*.json; do
  name=$(basename "$f" .json)

  # Build API envelope: set id=null so Grafana matches by UID, not numeric ID
  payload=$(python3 -c "
import json, sys
with open(sys.argv[1]) as fh:
    dash = json.load(fh)
dash['id'] = None
print(json.dumps({
    'dashboard': dash,
    'folderUid': sys.argv[2],
    'overwrite': True,
    'message': 'Synced from git',
}))
" "$f" "$FOLDER_UID")

  response=$(curl -s -w '\n%{http_code}' -u "${GRAFANA_AUTH}" \
    -X POST "${GRAFANA_URL}/api/dashboards/db" \
    -H "Content-Type: application/json" \
    -d "$payload")

  http_code=$(echo "$response" | tail -1)
  body=$(echo "$response" | head -n -1)

  if [[ "$http_code" == "200" ]]; then
    echo "  📊 Synced ${name}"
  else
    echo "  ❌ Failed ${name} (HTTP ${http_code}): ${body}" >&2
    (( errors++ ))
  fi
done

if (( errors > 0 )); then
  echo "❌ ${errors} dashboard(s) failed to sync" >&2
  exit 1
fi

echo "✅ All dashboards synced"
