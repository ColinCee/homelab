# 1. OpenObserve replaces Grafana, Prometheus and Loki

## Status

Accepted. The move's first step showed OpenObserve v1.1.2's alerting can carry the shapes of today's rules, with three workarounds ([#1208](https://github.com/ColinCee/homelab/issues/1208)).

## Context

The Beelink runs Grafana, Prometheus and Loki, with Alloy collecting ([observability](../observability.md)). That is three stores and a UI, and traces would add a fourth store. The Beelink is short of memory: CI VMs take most of it.

Seven setups were tested on one real day of homelab and Auto Kaizen data, replayed with fresh timestamps. The method and results are in [results.md](0001-openobserve/results.md).

A decision rule was fixed before any results:
1. The must-haves come first.
2. Then memory at the real rate: a setup 250 MB lighter than today's wins outright.
3. Otherwise simplicity decides.

## Decision

OpenObserve replaces Grafana, Prometheus and Loki. It is one program holding the store for logs, metrics and traces, the UI, logins and alerts. Alloy stays as the collector. The fallback is GreptimeDB or VictoriaMetrics + Grafana.

The finalists tied on memory at the real rate (OpenObserve 724-751 MiB, GreptimeDB + Grafana 723-755 MiB, over 3 rounds), so simplicity decided.

OpenObserve runs with `ZO_WAL_WRITE_QUEUE_ENABLED=false` and `ZO_WAL_FSYNC_DISABLED=false`. At its defaults it confirms writes before saving them, and a kill lost the last few seconds of data.

## Consequences

- One store and one UI replace three stores and a UI, and traces need no extra store.
- Disk is about 24 GB for 30 days, 2.3 times GreptimeDB's, within the 100 GB limit.
- After a crash it is back in 17-22 s, against 6 s for GreptimeDB.
- Today's 27 Grafana alert rules, the Home dashboard and Auto Kaizen's alert intake are rebuilt for OpenObserve. Its alerting differs from Grafana's in three ways, each met by a workaround the move builds:
  - A resolved message waits until the repeat interval ends. Alerts to the Control room repeat every minute, and the Control room is to ignore a repeat for an alert it already has open. "Control room unreachable" goes straight to Discord with a 30-minute repeat, so its resolved message can be up to 30 minutes late.
  - A failed query is only marked as an error in the alert list, and nothing is sent. The Control room is to poll the list instead of receiving today's evaluation-error alerts.
  - A query with no result counts as normal, so each no-data rule is written with `absent_over_time`.
- Several queries combine in one PromQL expression, and a log rule becomes SQL, whose message uses the aggregate value field.
- The webhook's message fields were checked; its custom header was set but not checked.
- Expiry of old data runs hours late. It is checked after the first month.
