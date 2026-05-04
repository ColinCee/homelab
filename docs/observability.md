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

The Agent Tasks dashboard derives task counts, status, duration, premium request,
and token totals from Loki `task_completed` events. Active worker counts still
come from Prometheus Docker container metrics.

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

Agent task outcomes are easiest to inspect through the shared `job="agent"`
label that Alloy applies to the API and worker containers:

```logql
{job="agent"} | json | event = `task_completed`
sum(count_over_time({job="agent"} | json | event = `task_completed` | status = `failed` [1h]))
sum(sum_over_time({job="agent"} | json | event = `task_completed` | unwrap premium_requests [1d]))
```

## Metrics: Prometheus

Prometheus receives metrics from:

- **Host:** Alloy's Unix exporter (`job="integrations/unix"` — Alloy overrides the configured `job_name`)
- **Containers:** Alloy's cAdvisor exporter (`job="docker"`)
- **CrowdSec:** direct scrape (`job="crowdsec"`)

Useful checks:

```bash
curl -sf http://beelink:8585/health
```

Useful PromQL:

```promql
count(container_last_seen{job="docker", name=~"worker-.*"}) OR vector(0)
```

Use the Agent Tasks dashboard or the LogQL examples above for task-level health.
Use Prometheus container metrics when you want raw worker container state.

## Alerts

Grafana alerting is provisioned from
`stacks/observability/provisioning/alerting/` and currently routes to the
`Discord Private` contact point.

The shipped rules cover host-level pressure such as:

- high CPU
- high RAM
- high disk usage

## Common debugging path

1. **Task looks stuck:** check `curl -sf http://beelink:8585/health`, inspect
   active worker containers with Prometheus, then check the current API and
   worker logs in Loki.
2. **A worker failed earlier:** query Loki by exact worker container name, such
   as `{container_name="worker-implement-118"}`.
3. **Failures are spiking:** graph `task_completed` failures in Loki and
   correlate with `{container_name=~"worker-(implement|review)-.*"} |= "ERROR"`.
