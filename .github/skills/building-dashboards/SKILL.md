---
name: building-dashboards
description: Edit homelab dashboards: Grafana's Home, from Git. Use when changing dashboard JSON or a panel's query.
---

# Dashboards

Grafana's Home is `stacks/observability/logs/<store>/home.json`, one per log store; compose mounts the
one `LOG_STORE` names. Until the unpicked store's folder goes, change both: they must be equal outside the
three log panels (datasource uid `logs`). Grafana loads the file on start and every 30 s, and refuses
a save from the UI: edit the file, not the UI. Open it at `http://grafana.internal/d/home` (save
`http://beelink.tail4f465e.ts.net:5080/d/home`).

## Home

The top answers "is anything wrong, and where?"; detail sits in the collapsed rows below, which load
only when opened. From the top:

1. Is anything wrong: a tile per service (Flight tracker, Home Assistant, DNS and proxy, Backup,
   Alerts), red when down, showing how long it has been up or down.
2. Headroom: CPU, memory, swap, disk and backup storage now with a trend over the time range, and
   the space free on the disk and under the backup storage's 8 GB.
3. Who uses the box: memory and CPU over 24 h, stacked by group: Monitoring, System and Apps.
4. Errors in the logs: error lines per hour by container, and the most repeated in the last hour.

The collapsed rows: Containers, Network, Backups, Alerts. Put a new panel in its section or row, or
add a row, rather than another dashboard. A heading is a row. A collapsed row keeps its panels in
its own `panels`, placed from the line under it; an open row's panels follow it at the top level.

Every number must read whole at 1200 px wide: the grid is 24 columns, so a tile 6 wide (a quarter)
has room for a title of about fifteen letters. No panel has its own time range; write a fixed window
into the query instead (`[7d]`, `_time:1h`).

## Colour and order

Each colour means one thing on every panel. The hexes live in `tests/test_dashboards.py` (`GROUPS`,
`NOW`, `WEEK`, `NEUTRAL`), which checks both files against them and against the containers compose
starts.

| Colour | Means |
|---|---|
| Blue, aqua, orange | Monitoring, System, Apps, and each container in its group's colour |
| Red | A service down, any alert firing, or a tile past a `level: now` rule's threshold |
| Amber | A tile past a `week` rule's threshold, or a warning with no rule. None means fine |
| Gray | One measure, or a series no list names (a removed container) |
| Blue, orange, aqua, yellow | Series of any other kind (Network), in that order |

- A tile's colour is its thresholds: base `text`, then a step at the rule's threshold (or base red
  or amber and a `text` step, for "under").
- A series' colour is a field override `byName` with a fixed colour; the default is fixed gray.
- Stacks keep a fixed order with the largest at the bottom: queries in that order, as refIds A, B, C.

## Queries

- Metric names are as Alloy sends them, mixed case included (`node_memory_MemAvailable_bytes`).
- Legends are `{{label}}`.
- Use `max by (name)` for container metrics and `max()` for a single host value, which removes
  duplicate series; `name!=""` selects containers. The CI VMs are not containers: their series
  are `job="ci-vms"`.
- A tile shows the last value; `graphMode: area` adds the trend.
- A top 10 is an instant query, `sort_desc(topk(10, ...))`, in a horizontal bar gauge.
- Alert history is Prometheus's own `ALERTS{alertstate="firing"}`, one sample per rule evaluation (a
  minute). A table over the time range uses `[$__range]` in instant queries, merged.
- Logs: Loki's stream label is `container_name`; `level` and the rest are structured metadata, and
  the line is the body. VictoriaLogs' stream field is `container_name`, the line `_msg`. Alloy resends
  a container's recent lines when it restarts: Loki drops an exact repeat, VictoriaLogs needs
  `uniq by (_time, container_name, _msg)` before `stats` (not allowed in a range query, which then
  counts repeats).

Check with `uv run python -m pytest tests/test_dashboards.py`.
