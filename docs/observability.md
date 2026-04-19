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
| **Prometheus** | Metrics storage for host, containers, CrowdSec, and agent metrics |
| **Loki** | Log storage queried with LogQL |
| **Alloy** | Scrapes metrics and ships Docker logs into Prometheus/Loki |
| **CrowdSec** | Security detections and firewall decisions, also exported as metrics |

Dashboards are managed via the Grafana HTTP API — JSON files in
`stacks/observability/dashboards/` are the source of truth and pushed to
Grafana by `scripts/sync-dashboards.sh` (runs automatically during deploy).

```bash
mise run sync:dashboards   # Push all dashboards to Grafana
```

- [Container Overview](../stacks/observability/dashboards/container-overview.json) — host gauges, container table, CPU/memory trends
- [Agent Tasks](../stacks/observability/dashboards/agent-tasks.json) — implement/review task metrics
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

That means worker containers are easiest to find by their deterministic names,
for example:

```logql
{container_name="worker-implement-118"}
{container_name="worker-review-42"}
{container_name=~"worker-(implement|review)-.*"} |= "ERROR"
```

The API removes stopped worker containers, but their log lines remain queryable
in Loki after they have been shipped, so historical worker runs are still
debuggable by `container_name`.

For the long-lived agent API container, first find the current container name in
Docker if needed:

```bash
docker ps --filter "name=agent" --format '{{.Names}}'
```

Then query it in Grafana Explore:

```logql
{container_name="<current-agent-container-name>"}
{container_name="<current-agent-container-name>"} |= "ERROR"
```

## Metrics: Prometheus

Prometheus receives metrics from:

- **Host:** Alloy's Unix exporter (`job="integrations/unix"` — Alloy overrides the configured `job_name`)
- **Containers:** Alloy's cAdvisor exporter (`job="docker"`)
- **CrowdSec:** direct scrape (`job="crowdsec"`)
- **Agent service:** `http://100.100.146.119:8585/metrics` (`job="agent"`)

Useful checks:

```bash
curl -sf http://beelink:8585/health
curl -sf http://beelink:8585/metrics | grep '^agent_'
```

Useful PromQL:

```promql
agent_task_in_progress
sum by (task_type) (increase(agent_task_total{status="failed"}[1h]))
sum by (task_type) (increase(agent_premium_requests_total[1d]))
```

Use `agent_task_total` and `agent_task_duration_seconds` from
`stacks/agents/app/metrics.py` when you want task-level health rather than raw
container health.

## Alerts

Grafana alerting is provisioned from
`stacks/observability/provisioning/alerting/` and currently routes to the
`Discord Private` contact point.

The shipped rules cover host-level pressure such as:

- high CPU
- high RAM
- high disk usage

## Common debugging path

1. **Task looks stuck:** check `curl -sf http://beelink:8585/health`, then
   inspect `agent_task_in_progress` and the current API container logs.
2. **A worker failed earlier:** query Loki by exact worker container name, such
   as `{container_name="worker-implement-118"}`.
3. **Failures are spiking:** graph
   `sum by (task_type) (increase(agent_task_total{status="failed"}[1h]))` and
   correlate with `{container_name=~"worker-(implement|review)-.*"} |= "ERROR"`.
