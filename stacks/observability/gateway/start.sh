#!/bin/sh
# Turns each login into the Basic header the Caddyfile compares, then runs Caddy. Only this container holds
# root's OpenObserve password; a missing password stops it, since an empty one would match an empty login.
set -eu
auth() { printf '%s:%s' "$1" "$2" | base64 | tr -d '\n'; }
ROOT_AUTH=$(auth root@homelab.internal "${OPENOBSERVE_ROOT_PASSWORD:?}")
STATS_AUTH=$(auth stats@homelab.internal "${OPENOBSERVE_STATS_PASSWORD:?}")
AGENTS_AUTH=$(auth agents@homelab.internal "${OPENOBSERVE_AGENTS_PASSWORD:?}")
export ROOT_AUTH STATS_AUTH AGENTS_AUTH
unset OPENOBSERVE_ROOT_PASSWORD OPENOBSERVE_STATS_PASSWORD OPENOBSERVE_AGENTS_PASSWORD
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
