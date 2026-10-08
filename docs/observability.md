# Observability

Start here when a service is unhealthy. Compose, `config.alloy`, and Grafana
provisioning under `stacks/observability/` own exact settings and retention.
Alloy collects both logs and metrics to avoid separate collectors. Tracing and
multi-tenant metrics infrastructure are unnecessary for this single host.

Monitoring on Beelink cannot detect its own complete outage from outside.
Retain an independent external heartbeat when host-outage detection is needed;
do not treat an on-host dashboard as proof of availability.

The [deploy watchdog](../.github/workflows/deploy-watchdog.yaml) runs every 15
minutes on GitHub-hosted runners and posts to Discord when a `deploy.yaml` run
has been queued for over 15 minutes, which usually means the `beelink` runner
is down. It posts once when a problem starts and once when it clears, using its
previous run's conclusion as state. Healthchecks.io remains the external
heartbeat for full host outages.

## What's deployed

| Component | What it does |
|-----------|---------------|
| **Grafana** | Dashboards, Explore, and alert routing |
| **Prometheus** | Metrics storage for host, containers, and CrowdSec |
| **Loki** | Log storage queried with LogQL |
| **Alloy** | Scrapes metrics and ships Docker logs into Prometheus/Loki |
| **CrowdSec** | Security detections and firewall decisions, also exported as metrics |

Grafana provisions dashboards directly from the read-only mounted JSON files in
`stacks/observability/dashboards/`. Edit those files and deploy; Grafana polls for
updates. UI edits are disabled so Git remains the source of truth.

- [Container Overview](../stacks/observability/dashboards/container-overview.json) — host gauges, container table, CPU/memory trends
- [Security](../stacks/observability/dashboards/security.json) — CrowdSec detections and firewall decisions
- [Backups](../stacks/observability/dashboards/backups.json) — off-site backup successes, failures, stored size and run log

Grafana allows anonymous read-only (Viewer) access so dashboards open without
a login, including for agents checking a change. Editing, Explore and
administration still require the admin password. This relies on the port
being bound to the Tailscale address and on the tailnet policy limiting who
can reach it; remove `GF_AUTH_ANONYMOUS_*` if either changes.

### Dashboard patterns

Use `max by (name)` for container metrics or `max()` for a single host value
where deduplication is needed. Alloy recreation changes the `instance` label;
old series remain temporarily visible and can double-count values.

Datasource UIDs are pinned to `prometheus` and `loki` in
`provisioning/datasources/datasources.yaml`. Grafana does not update
UIDs on existing datasources via provisioning. If they drift, inspect the
provisioned and live datasource identities before making changes; do not
blindly edit Grafana's database.

## Logs: Loki via Alloy

Alloy discovers Docker containers through a read-only socket proxy, adds a `container_name`
label, and forwards logs to Loki (`stacks/observability/config.alloy`).

To inspect a service, find its current container name and query it in Grafana
Explore:

```bash
docker ps --format '{{.Names}}'
```

```logql
{container_name="<current-container-name>"}
{container_name="<current-container-name>"} |= "ERROR"
```

## Metrics: Prometheus

Prometheus receives metrics from:

- **Host:** Alloy's Unix exporter (`job="integrations/unix"` — Alloy overrides the configured `job_name`)
- **Containers:** Alloy's cAdvisor exporter (`job="docker"`)
- **CrowdSec:** direct scrape (`job="crowdsec"`)
- **Flight tracker:** Alloy's blackbox exporter probes the public
  `https://api.colincheung.dev/aircraft` every minute
  (`job="integrations/blackbox/flight-tracker"`). `probe_success` is 1 only
  when the response says `"apiHealth":"live"`, so tunnel, backend and
  upstream data source failures all show as 0.

Useful checks:

```bash
curl -sf http://100.100.146.119:3001/api/health
curl -sf http://100.100.146.119:6060/metrics >/dev/null
```

Useful PromQL:

```promql
max by (name) (container_last_seen{job="docker", name!=""})
max(crowdsec_acquisition_source_hits_total{job="crowdsec"})
```

## Alerts

Grafana alerting is provisioned from
`stacks/observability/provisioning/alerting/` and currently routes to the
`Discord Private` contact point. That contact point also has a webhook to the
"Homelab alert triage" Claude routine, which investigates firing alerts and
reports in the project thread (resolves go to Discord only). The deploy
watchdog wakes the same routine when a deploy stalls. Both need the
`CLAUDE_ROUTINE_TOKEN` secret (claude.ai/code/routines → routine → API
trigger → Generate token); without it the webhook gets a 401 and Discord is
unaffected.
After deploying alerting file changes, reload them with Grafana's authenticated
`POST /api/admin/provisioning/alerting/reload` API; unlike dashboards, these files
are not polled. A Grafana-only restart also reloads them.

Grafana's root URL uses the Tailscale IP rather than the bare `beelink`
hostname. Discord validates the URL in Grafana's alert embed and rejects
host-only URLs with HTTP 400.

The shipped rules cover host-level pressure such as:

- high CPU
- high RAM
- high disk usage
- **Flight Tracker Down**: the live-data probe has failed for 10 minutes

Host RAM uses `node_memory_MemAvailable_bytes`; `node_memory_AvailableBytes`
does not exist in Alloy's Unix exporter. Verify the rule's exact query in
Prometheus when a dashboard has data but an alert does not.

Discord uses the provisioned `homelab-discord` notification template in one
embed. Threshold alerts show an evaluated percentage and threshold duration.
No-data and evaluation errors remain separate alerts, grouped by affected rule;
they mean monitoring is unavailable, not that a resource threshold was crossed.
Resolved messages describe an ended alert, not proof of recovery. Check the
current evaluation before declaring the resource healthy. Tests are explicitly
labelled and do not represent resource incidents.

## Common debugging path

1. **A service is unavailable:** check its Compose status, then inspect the
   current container logs in Loki.
2. **Metrics are stale:** check Alloy first, then Prometheus targets and the
   container metrics in Grafana.
