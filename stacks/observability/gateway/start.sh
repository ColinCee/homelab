#!/bin/sh
# Turns each login into the Basic header the Caddyfile compares, then runs Caddy. A missing password stops it, since
# an empty one would match an empty login.
set -eu
auth() { printf '%s:%s' "$1" "$2" | base64 | tr -d '\n'; }
STATS_AUTH=$(auth stats@homelab.internal "${STATS_PASSWORD:?}")
AGENTS_AUTH=$(auth agents@homelab.internal "${AGENTS_PASSWORD:?}")
export STATS_AUTH AGENTS_AUTH
unset STATS_PASSWORD AGENTS_PASSWORD
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
