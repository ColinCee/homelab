# 1. OpenObserve replaces Grafana, Prometheus and Loki

## Status

Proposed. It becomes accepted once the move's first step shows OpenObserve's alerting can carry today's rules ([#1208](https://github.com/ColinCee/homelab/issues/1208)).

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
- Today's 27 Grafana alert rules, the Home dashboard and Auto Kaizen's alert intake are rebuilt for OpenObserve. Its docs leave open some features they use, which the move's first step must prove:
  - rules combining several queries;
  - alerts on missing data and on evaluation errors;
  - recovery messages.

  If that step fails, this record is rejected and a Grafana-based fallback is used instead, keeping the rules almost unchanged.
- Expiry of old data runs hours late. It is checked after the first month.
