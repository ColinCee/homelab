---
name: dashboards
description: Edit homelab dashboards, OpenObserve's Home from Git and Grafana's until the cutover. Use when changing dashboard JSON or a panel's query.
---

# Dashboards

OpenObserve's Home is `stacks/observability/openobserve/dashboards/home.json`. On each deploy
`openobserve-apply` makes OpenObserve's `homelab` dashboard folder hold that folder's files, keyed by
title (`apply_dashboards` in `apply.py`), and deletes one Git removed. Edit the file, not the UI.
Open it at `http://openobserve.internal` (save `http://beelink.tail4f465e.ts.net:5080`).

## Home

The top answers "is anything wrong, and where?"; detail sits in the other tabs, which load only
when opened. The Home tab, from the top:

1. Is anything wrong: a tile per service (Flight tracker, Home Assistant, DNS and proxy, Backup,
   Alerts), red when down, showing how long it has been up or down.
2. Headroom: CPU, memory, swap, disk and backup storage now with a trend over the time range, and
   when the disk and the backup storage fill at their 7-day rate.
3. Who uses the box: memory and CPU over 24 h, stacked by group: Monitoring, System and Apps.
4. Errors in the logs: error lines per hour by container, and the most repeated in the last hour.

The other tabs: Containers, Network, Backups, Alerts. Put a new panel in its section or
tab, or add a tab, rather than another dashboard. Group numbers that belong together under a
heading, and size each panel to its content, with no empty space around its values.

Every number must read whole at 1200 px wide:
- A panel's header keeps about 210 px for its icons and refresh time, so a tile 48 columns wide
  (a quarter) has room for a title of about seven letters, and 64 columns for about fifteen.
- A heading is a markdown panel 2 rows tall whose title is the text; a markdown body shows only
  from 4 rows tall.
- No panel has its own time range: OpenObserve draws a time picker on it, which hides a tile's
  value. Write a fixed window into the query instead (`_timestamp >= ...` in SQL, `[7d]` in PromQL).

## Colour and order

Each colour means one thing on every tab. The hexes live in `tests/test_openobserve_alerts.py`
(`GROUPS`, `NOW`, `WEEK`, `NEUTRAL`), which checks the file against them.

| Colour | Means |
|---|---|
| Blue, aqua, orange | Monitoring, System, Apps, and each container in its group's colour |
| Red | A service down, any alert firing, or a tile past a `level = "now"` rule's threshold |
| Amber | A tile past a `"week"` rule's threshold, or a warning with no rule. None means fine |
| Gray | One measure (sparklines, Stored over time), or a series Git no longer has |
| Blue, orange, aqua, yellow | Series of any other kind (Network), in that order |

- A tile's colour is a value mapping (`gt` or `lt`; the last match wins) with a colour and no text.
- A series' colour is `config.color.colorBySeries` by name, with `mode: "fixed"` and a gray
  `fixedColor` for any series not listed. Without a mode, unlisted series get a value gradient.
- Bars run largest first. Stacks keep a fixed order with the largest at the bottom, and the legend
  follows: PromQL series come in query order, SQL series in the order rows first name them.
  OpenObserve runs a SQL chart in time slices (the current hour is its own), so a series new in
  the newest slice comes first whatever its total.
- Tables sort by their obvious column: the count, or the newest first.

## Queries

- Metric names are lower case (`node_memory_memavailable_bytes`).
- A matcher like `=""` cannot select a series that lacks the label; `name!=""` works. Use
  `max by (name)` for container metrics, as `docs/observability.md` says.
- Every PromQL panel runs a range query over the dashboard's time: OpenObserve drops a query's
  `query_type`. A tile shows the last value; a `sparkline` adds the trend.
- A top 10 is SQL on the metric's stream (`ORDER BY y_axis_1 DESC LIMIT 10`; an `h-stacked` panel
  draws the first row at the top). PromQL bars come in label order: `sort_desc` does nothing in a
  range query, and a PromQL top 10 must pin `@ end()` or each step picks its own ten.
- OpenObserve drops an `ORDER BY` on an expression it does not select: select it (`sum(y_axis_1)
  OVER (PARTITION BY breakdown_1) AS total`) and order by its name.
- Logs are the `containers` stream. Alloy resends a container's old lines when it restarts, so
  count `DISTINCT _timestamp, body`; filter with `match_all` words (the text index) before a
  `regexp_like`, which is slow over a day of logs.
- Alert states are the `triggers` stream (`module = 'alert'`): a firing rule logs `firing`, or
  `notify_failed` when its post fails.
- Before running `apply.py` by hand, rebase on main: it makes OpenObserve match the files you
  have, so an old checkout reverts newer alerts.

To try a change live, from a tab signed in to OpenObserve PUT the whole file to
`/api/default/dashboards/<id>?folder=<id>&hash=<hash>` (both ids and the hash from
`GET /api/default/dashboards?folder=<id>`). Check with
`uv run python -m pytest tests/test_openobserve_alerts.py`.

## Grafana

Until the cutover Grafana's Home is `stacks/observability/dashboards/home.json`, which Grafana
polls. Keep its UID, set `id` to `null`, and use datasource UIDs `prometheus` and `loki`.
