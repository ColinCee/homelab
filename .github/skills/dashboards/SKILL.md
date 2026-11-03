---
name: dashboards
description: Edit homelab Grafana dashboards. Use when changing dashboard JSON or PromQL panels.
---

# Grafana dashboards

1. Read the dashboard JSON in `stacks/observability/dashboards/` and datasource
   definitions in `stacks/observability/provisioning/datasources/`.
2. Keep existing dashboard UIDs and set `id` to `null`. Use datasource UIDs
   `prometheus` and `loki`, not numeric IDs.
3. Follow the query labels and deduplication guidance in
   `docs/observability.md`; inspect `config.alloy` for the actual series.
4. Validate JSON and inspect changed panels in Grafana after an approved
   deployment. Grafana polls the mounted JSON files; no API upload is needed.

Edit source JSON, not dashboard copies in the Grafana UI.

There is one dashboard, `home.json`, at the top level with no folder (Colin,
3 Nov 2026): put new panels in its sections, and detail in a collapsed row,
rather than adding a dashboard.
