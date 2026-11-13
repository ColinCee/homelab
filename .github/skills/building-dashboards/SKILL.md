---
name: building-dashboards
description: Edit or design homelab dashboards: Grafana's pages, from Git. Use when changing dashboard JSON, a panel's query, or how a page looks.
---

# Dashboards

Grafana's pages are the files in `stacks/observability/grafana/dashboards/`, each named for its `uid`.
Grafana reloads them every 30 s and refuses a save from the UI: edit the file. A page's link is
`http://grafana.internal/d/<uid>`.

Adding or restyling a panel or a page: read [DESIGN.md](DESIGN.md) first, for the panel each job takes,
the shapes that hold on a wide screen, how to look at it with real data, and what was dropped.

## Pages

A panel goes on the page whose question it answers.

| Page | Question |
|---|---|
| `home` | Is anything wrong? One screen, no scrolling |
| `server` | The box: has it got room? Network and backups too |
| `services` | What runs on it: is each running, and what does it use and complain about? |
| `ci` | Is CI fast, and is it getting slower? |
| `logs` | What are the services saying? Pick containers, search words |
| `traces` | What did each service answer, how long did it take, and who fills the store? |

- Every page carries the same `links`, one to each page.
- A Home tile opens the page that explains it: `fieldConfig.defaults.links`, `oneClick`, `/d/<uid>`.
- Home's service tiles are `instant` queries named Up and Down: over a range the state it left
  would still show.
- A collapsed row keeps its panels in its own `panels`, below its line; an open row's follow it at
  the top level.

No panel has its own time range; write a window into the query (`[7d]`, `_time:1h`).

## Colour and order

Each colour means one thing; hexes are in `tests/test_dashboards.py`.

| Colour | Means |
|---|---|
| Blue, aqua, orange | Monitoring, System, Apps, and each container in its group's colour |
| Red | A service down or stopped, any alert firing, or a tile past a `level: now` rule's threshold |
| Amber | A tile past a `week` rule's threshold, or a warning with no rule |
| Green | Fine: a filled tile, a dial, a history line, a running service |
| Gray | One measure, or a series no override names |
| Blue, orange, aqua, yellow | Any other series (Network, CI's runners), in that order |

- A tile's colour is its thresholds: base `text` (green when filled), then a step at the rule's
  threshold (or base red or amber and a `text` step, for "under").
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
- Alert history is `ALERTS{alertstate="firing",alertname!="watchdog"}` (it always fires), a
  sample a minute. A table over the time range uses `[$__range]` in instant queries, merged.
- Logs: VictoriaLogs' stream field is `container_name`, the line `_msg`. Alloy resends a container's
  lines when it restarts, so use `uniq by (_time, container_name, _msg)` before `stats` (not
  allowed in a range query, which then counts repeats).
- Spans (`uid: spans`): the trace store in LogsQL. `duration` is nanoseconds, a field
  with a colon is quoted (`"span_attr:event":*`), and a span's attributes tell a run from a job, a
  step or a test (`ci-runner/run-times/ci_times.py`).

Check: `uv run pytest tests/test_dashboards.py`.
