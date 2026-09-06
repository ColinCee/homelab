# Observability

This is the "where do I look first?" page. For the design rationale, see
[ADR-003](decisions/003-observability.md). For exact scrape and log pipeline
config, use `stacks/observability/compose.yaml`,
`stacks/observability/config.alloy`, and the Grafana provisioning files as the
authoritative sources.

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

### Dashboard patterns

All queries use `max by (name)` (container metrics) or `max()` (host
metrics) to deduplicate series. When Alloy is recreated, its Prometheus
`instance` label changes but old series persist until the staleness
window expires — without aggregation, every metric appears twice.

Datasource UIDs are pinned to `prometheus` and `loki` in
`provisioning/datasources/datasources.yaml`. Grafana does not update
UIDs on existing datasources via provisioning — if they drift, fix the
SQLite DB directly.

## Logs: Loki via Alloy

Alloy discovers Docker containers from the socket, adds a `container_name`
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

Knowledge commands and backups also write structured events to journald. Use
the service-specific labels when inspecting those runs:

```logql
{job="knowledge", service="ingest"} | json | event = `task_completed`
{job="knowledge", service="save"} | json | event = `task_completed`
{job="knowledge", service="backup"} | json | event = `knowledge_backup_completed`
```

## Metrics: Prometheus

Prometheus receives metrics from:

- **Host:** Alloy's Unix exporter (`job="integrations/unix"` — Alloy overrides the configured `job_name`)
- **Containers:** Alloy's cAdvisor exporter (`job="docker"`)
- **CrowdSec:** direct scrape (`job="crowdsec"`)

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
`Discord Private` contact point.

The shipped rules cover host-level pressure such as:

- high CPU
- high RAM
- high disk usage

## Common debugging path

1. **A service is unavailable:** check its Compose status, then inspect the
   current container logs in Loki.
2. **Knowledge ingest or save failed:** inspect the matching `job="knowledge"`
   journald stream and verify the Postgres container is healthy.
3. **Metrics are stale:** check Alloy first, then Prometheus targets and the
   container metrics in Grafana.
