# Public stats

The home page of colincheung.dev shows a few live numbers about this server:
whether its stacks are up, how many addresses CrowdSec blocks, how many aircraft
the flight tracker follows, Home Assistant's uptime and whether the last backup
ran. This page says how they get there and what that exposes.

## How it works

```
Beelink: public-stats container (observability stack)
  │  every 5 min: Prometheus on the stack network, the public flight-tracker API
  │  PUT, signed with the server's private key, outbound HTTPS
  ▼
colincheung.dev/homelab.json   (the blog's Worker, in the blog repo)
  │  checks the signature, the shape and that it is newer; stores it in KV
  ▼
GET /homelab.json   (the home page fetches it)
```

Push, not pull: nothing on the Beelink accepts a new connection. The container
has no ports and talks only to Prometheus and, outbound, to the flight-tracker
API (already public) and the blog. The script is
[`public_stats.py`](../stacks/observability/public_stats.py).

## What is published

Only these fields; the Worker rejects a body with any other key or type:

| Field | Meaning | Source |
|---|---|---|
| `at` | when the numbers were gathered (Unix seconds) | the container's clock |
| `allUp` | every stack has a running container | `container_last_seen` within the last minute |
| `blocked` | addresses on CrowdSec's blocklist | `cs_active_decisions` |
| `tracking` | aircraft in the tracker's London box now | `/aircraft` KPIs |
| `londonHour` | aircraft headed for London seen in the past hour | the same KPIs |
| `haDays` | whole days since Home Assistant's container started | `container_start_time_seconds` |
| `backupOk` | a backup succeeded in the last 26 hours | `homelab_backup_last_success_timestamp_seconds` |

They are coarse on purpose. One flag for all stacks, not one each, so a stopped
CrowdSec is not announced; uptime in days, so restart times are not; backup as
yes or no. No hostnames, addresses, container IDs or versions leave the host:
the body is built from named numbers, never by forwarding a Prometheus response.
When Prometheus reports no containers at all, or no CrowdSec count, the script
skips the push rather than publish "all down" or a blank that singles out
CrowdSec; the last numbers age instead. After a failed push it tries again in
15 seconds, so a deploy's readiness wait sees the container healthy. The flight numbers are `null` unless the tracker's data
is live.

The Worker accepts `at` up to 10 minutes old and a minute ahead, so a
drifting clock shows as refused pushes in the container's log (`docker logs
observability-public-stats-1`) and an unhealthy container.

## Limits

- Auth is one Ed25519 key pair. The private key's only copy is this repo's
  `PUBLIC_STATS_KEY` secret (32 bytes as hex); the public half is
  `HOMELAB_PUBLIC_KEY` in the blog's `wrangler.json`, not a secret. A stolen
  private key could only post these numbers; a leaked blog repo or Worker gives
  nothing. The Worker takes only numbers newer than the ones it has, so an old
  signed body cannot be replayed. To set it up or rotate it, run
  `uv run python scripts/public_stats_key.py` yourself (it stores the private
  key as the secret and prints the public one), commit the public key to the
  blog's `wrangler.json`, then redeploy both.
- The container's image is built on the host from
  [`public-stats.Dockerfile`](../stacks/observability/public-stats.Dockerfile)
  (Python plus `cryptography`, since the standard library has no Ed25519).
  Renovate updates the base image; keep the `cryptography` pin in step with
  the dev dependency in `pyproject.toml`.
- Every GET runs the Worker (KV is read with a 60 s cache, and browsers cache
  the response for 60 s), so a flood of requests could use up the free plan's
  100,000 Worker requests a day. Then only `/homelab.json` fails until midnight
  UTC and the page says the numbers are offline; the rest of the site is static
  assets and does not run the Worker. KV writes are 288 a day of the free 1,000.
- When the Beelink is down, the last numbers stay and the page shows their age;
  past 20 minutes the dots go grey instead of green.
