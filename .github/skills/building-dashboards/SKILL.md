---
name: building-dashboards
description: Edit homelab dashboards: Grafana's pages, from Git. Use when changing dashboard JSON or a panel's query.
---

# Dashboards

Grafana's pages are the files in `stacks/observability/grafana/dashboards/`, each named for its `uid`.
Grafana reloads them every 30 s and refuses a save from the UI: edit the file. A page's link is
`http://grafana.internal/d/<uid>`.

## Pages

A panel goes on the page whose question it answers; a new page needs a question none covers.

| Page | Question |
|---|---|
| `home` | Is anything wrong? One screen, no scrolling |
| `server` | Has the box got room? Backups too |
| `services` | Is each service running, and what is it complaining about? Alerts too |
| `ci` | Is CI fast, and is it getting slower? |

- Every page carries the same `links`: each page, then Explore's logs and traces.
- A Home tile opens the page that explains it: `fieldConfig.defaults.links`, `oneClick`, `/d/<uid>`.
- Home's service tiles are `instant` queries named Up and Down: over a range the state it left
  would still show.
- A heading is a row. A collapsed row keeps its panels in its own `panels`, placed from the line
  under it; an open row's follow it at the top level.

The grid is 24 columns: at 1200 px a tile 6 wide holds a title of about fifteen letters. No panel
has its own time range; write a window into the query (`[7d]`, `_time:1h`).

## Colour and order

Each colour means one thing on every panel; hexes are in `tests/test_dashboards.py`, which checks
every page.

| Colour | Means |
|---|---|
| Blue, aqua, orange | Monitoring, System, Apps, and each container in its group's colour |
| Red | A service down or stopped, any alert firing, or a tile past a `level: now` rule's threshold |
| Amber | A tile past a `week` rule's threshold, or a warning with no rule. None means fine |
| Gray | One measure, a running service, or a series no override names |
| Blue, orange, aqua, yellow | Series of any other kind (Network, CI's runners), in that order |

- A tile's colour is its thresholds: base `text`, then a step at the rule's threshold (or base red
  or amber and a `text` step, for "under").
- A series' colour is a field override with a fixed colour: `byName`, or for containers the two
  `byRegexp` patterns by stack, which a new app stack joins. The default is fixed gray.
- Stacks keep a fixed order with the largest at the bottom: queries in that order, as refIds A, B, C.

## Queries

- Metric names are as Alloy sends them, mixed case included (`node_memory_MemAvailable_bytes`).
- Use `max by (name)` for container metrics and `max()` for a single host value, which removes
  duplicate series; `name!=""` selects containers. The CI VMs are not containers: their series
  are `job="ci-vms"`.
- A running service: the `container-stopped` rule's join of `homelab_container_expected`.
- `homelab_ci_job_seconds` is each job's latest good run in this repo, held until the next: a median
  over it weighs hours, not runs.
- A tile shows the last value; `graphMode: area` adds the trend.
- A top 10 is an instant query, `sort_desc(topk(10, ...))`, in a horizontal bar gauge.
- Alert history is `ALERTS{alertstate="firing",alertname!="watchdog"}` (the watchdog always fires), a
  sample a minute. A table over the time range uses `[$__range]` in instant queries, merged.
- Logs: VictoriaLogs' stream field is `container_name`, the line `_msg`. Alloy resends a container's
  recent lines when it restarts, so use `uniq by (_time, container_name, _msg)` before `stats` (not
  allowed in a range query, which then counts repeats).

Check: `uv run python -m pytest tests/test_dashboards.py`.
