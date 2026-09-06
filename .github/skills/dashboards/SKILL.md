---
name: dashboards
description: Edit homelab Grafana dashboards. Use when changing dashboard JSON or PromQL panels.
---

# Grafana dashboards

1. Read the dashboard JSON in `stacks/observability/dashboards/` and datasource
   definitions in `stacks/observability/provisioning/datasources/`.
2. Keep existing dashboard UIDs and set `id` to `null`. Use datasource UIDs
   `prometheus` and `loki`, not numeric IDs.
3. Deduplicate container metrics with `max by (name) (...)` and host metrics
   with `max(...)` where a single value is needed. Alloy recreation changes
   the `instance` label, leaving old series temporarily visible.
4. Use `job="docker"` for containers, `job="integrations/unix"` for host metrics,
   and `job="crowdsec"` for security metrics.
5. Validate JSON and inspect changed panels in Grafana after an approved
   deployment. Grafana polls the mounted JSON files; no API upload is needed.

Edit source JSON, not dashboard copies in the Grafana UI.
