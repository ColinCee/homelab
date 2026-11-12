#!/bin/sh
# Turns each login into the Basic header the Caddyfile compares, then runs Caddy. A missing or empty password stops
# it, since an empty one would match an empty login. SECRETS_DIR and CADDYFILE let tests/test_gateway.py run it as is,
# outside the container.
set -eu
secrets=${SECRETS_DIR:-/run/secrets}
auth() { printf '%s:%s' "$1" "$2" | base64 | tr -d '\n'; }
stats=$(cat "$secrets/stats_password")
agents=$(cat "$secrets/agents_password")
STATS_AUTH=$(auth stats@homelab.internal "${stats:?stats_password is empty}")
AGENTS_AUTH=$(auth agents@homelab.internal "${agents:?agents_password is empty}")
export STATS_AUTH AGENTS_AUTH
exec caddy run --config "${CADDYFILE:-/etc/caddy/Caddyfile}" --adapter caddyfile
