---
applyTo: "stacks/observability/dashboards/**"
---

# Dashboard Conventions

Dashboards are JSON files in `stacks/observability/dashboards/`. They are the
source of truth and pushed to Grafana via `scripts/sync-dashboards.sh` (HTTP
API, not file provisioning).

## Editing workflow

1. Edit the JSON file
2. Run `mise run sync:dashboards` to push (or deploy the observability stack)
3. Verify in Grafana at `http://100.100.146.119:3001`

No Grafana restart is needed — the sync script pushes via `POST /api/dashboards/db`.

## Dashboard JSON requirements

- **`"id": null`** — Grafana matches dashboards by UID, not numeric ID. A non-null
  ID causes upsert failures or updates the wrong dashboard.
- **`"uid": "<stable-uid>"`** — each dashboard has a stable UID (e.g., `container-overview`,
  `agent-tasks`, `security`). Never change these.
- **Datasource references** — always use `{"type": "prometheus", "uid": "prometheus"}`
  or `{"type": "loki", "uid": "loki"}`. These UIDs are pinned in
  `provisioning/datasources/datasources.yaml`.

## Metric and log job labels

| Source | Job label | Use |
|--------|-----------|-----|
| Host metrics (CPU, RAM, disk, uptime) | `job="integrations/unix"` | Prometheus via Alloy's `prometheus.exporter.unix` (overrides configured `job_name`) |
| Container metrics (CPU, memory, network, active workers) | `job="docker"` | Prometheus via Alloy's `prometheus.exporter.cadvisor` |
| CrowdSec metrics | `job="crowdsec"` | Prometheus direct scrape |
| Agent task logs | `job="agent"` | Loki Docker logs with `task_completed` events |

Agent task counts, status, duration, premium request, and token panels should use
LogQL over the `task_completed` events in `{job="agent"}`. Active worker panels
still use Prometheus Docker container metrics because they describe live
container state rather than terminal task outcomes.

## Scraper instance deduplication

When Alloy is recreated (new container ID), its `instance` label changes but
old series persist in Prometheus until staleness expires. Without aggregation,
every metric appears twice.

**Required patterns:**
- Container metrics: `max by (name) (metric{job="docker", name!=""})` — dedup by container name
- Host metrics: `max(metric{job="integrations/unix"})` — no useful label to group by
- `avg()` and `sum()` with full aggregation handle this implicitly

Never use a raw selector without aggregation for stat/gauge panels.

## Container name regex

Timeseries panels use `(.+)-1$` in legend/value transforms to strip the Docker
Compose replica suffix (e.g., `observability-grafana-1` → `observability-grafana`).

This regex intentionally only matches `-1` (not `-\d+$`) to preserve meaningful
numbers in worker container names like `worker-implement-141`.

## Panel types

| Type | When to use |
|------|-------------|
| `gauge` | Single metric with known min/max (CPU %, RAM bytes with capacity, disk) |
| `stat` | Single metric without a meaningful range (uptime, container count) |
| `table` | Multi-row data (container list with multiple columns) |
| `timeseries` | Trends over time |
