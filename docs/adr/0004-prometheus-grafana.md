# 4. Prometheus, Grafana and a picked log and trace store replace OpenObserve

## Status

Proposed. Supersedes 0001 once accepted. [ADR 0002](0002-alertmanager.md) stands, with Prometheus sending to Alertmanager in OpenObserve's place.

## Context

OpenObserve ([ADR 0001](0001-openobserve.md)) needs code and services of its own to meet the alert standard and read-only logins: `apply.py` for rules and dashboards from Git and for failed queries, a hold written into every query, and a gateway for read-only logins. Agents cannot read its logs, and it stores a metric reading in about 12 B against Prometheus's 2.4 B.

Six whole stacks were tested side by side on the Beelink's live feed, with a pick rule fixed before any result: no must-have failed, then the fewest workarounds (3 points for each service or code written for it, 1 for each rule, panel or login that cannot be carried over unchanged), then the fewest programs, then memory if 250 MiB apart, then disk, then maturity. Method and numbers: [results.md](0004-prometheus-grafana/results.md).

| Stack | Workaround points | Programs | Memory, MiB |
|---|---|---|---|
| OpenObserve, as it runs | 12 | 4 | 337-347 |
| Prometheus + VictoriaLogs + Grafana + Alertmanager | 5 | 6 | 345-362 |
| Prometheus + Loki + Grafana + Alertmanager | 6 | 6 | 387-403 |
| VictoriaMetrics + VictoriaLogs + VictoriaTraces | 5 | 7 | 369-381 |

GreptimeDB failed a must-have (its `absent_over_time` returns nothing, so no-data rules never fire), and OpenObserve behind Grafana let a Viewer delete a log stream through its only server-side data source.

## Decision

Prometheus stores metrics and runs the rules, Grafana shows every store from files in Git, Alertmanager posts to Discord, and Alloy collects. One Caddy gateway is the only way to read a store: Grafana's data sources, agents, the heartbeat and public stats all log in there, and each login reaches only read paths. Every store tested serves writes or admin calls beside its reads (a Viewer's GET through Grafana shut Loki down; a Viewer's POST wrote into VictoriaLogs), and none has read-only users, so the gateway is the one place that keeps readers to reading.

The log store and the trace store are each one of two, picked by `LOG_STORE` (Loki or VictoriaLogs) and `TRACE_STORE` (Tempo or VictoriaTraces) in the stack's `.env.example`. Each store's folder holds all that differs: its service, its Alloy exporter, its gateway paths, its Grafana data source and, for logs, the Home dashboard in its query language.

The pick, by the same rule, is VictoriaLogs for logs, and for traces VictoriaTraces, by a narrow margin a measurement at the real rate settles; `.env.example` sets both:

- Logs: both stores pass the musts and tie on workarounds and programs, so memory decides: VictoriaLogs held a median 363 MiB through a 30-day backfill and heavy queries, Loki 1,016 MiB (killed at a 1 GiB cap). VictoriaLogs also took 2.2 times less disk and searched 2.6 to 8.5 times faster over 7 and 30 days.
- Traces: both stored every span at ten times the real rate. Stock Tempo let a Viewer read its config and change its state through Grafana, but its own `http_api_prefix` keeps Grafana to its query API, so both need no workaround and run as one program. Tempo is a finished release where VictoriaTraces is not, which counts only as a nice for traces; VictoriaTraces held at most 106 MiB where Tempo reached 734 MiB on searches over every trace, and takes about a fifth of the disk per span.

Both picks rest on short runs, not the real rate over days; the 7-day run beside the live stack checks memory, crash loss and expiry before this record is accepted.

## Consequences

- No code of our own runs the alerts: the rules are a Prometheus file tested with `promtool`, and the hold, per-series clears, no-data and the hold-back come from Prometheus and Alertmanager.
- Agents read logs and traces as well as metrics, read-only.
- Six programs to keep upgraded instead of four.
- Metric and log history starts again at the switch; OpenObserve's volume is kept for a rollback ([switching-observability.md](../runbooks/switching-observability.md)).
- Until the pick, two Home dashboards are kept in step; the other store's folder goes once it is made.
