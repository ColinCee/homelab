# 1. Observability stack

## Status

Accepted.

## Context

The Beelink collects metrics, logs and traces and alerts to Discord, on a host whose memory goes mostly to CI VMs. How it runs: [observability](../observability.md).

OpenObserve, one program holding all three signals with its own UI and alerts, came first in a bake-off of seven setups on a replayed day of data. That test was wrong in three ways: the Grafana stack it replaced ran untuned and with Tempo, which sank it (without Tempo it was estimated at 684 MiB from one round, against OpenObserve's 724-751 MiB); OpenObserve's collector had the wrong login in every scored round; and disk only had to fit 30 days in 100 GB, so a disk gap could not count against it.

Running it showed the cost:

- The alert standard ([observability](../observability.md), "Alerts") and read-only logins needed code and services of its own: Alertmanager in front of Discord, a script loading rules and dashboards from Git and catching failed queries, a hold written into every query, and a gateway for read-only logins.
- The agents' login could not read its logs.
- It stored a metric reading in 12.4 B, five times Prometheus's 2.4 B.

So six whole stacks were tested side by side on the Beelink's live feed, then each log and trace store on its own, with a pick rule fixed before any result: no must-have failed, then the fewest workarounds, then the fewest programs, then memory if 250 MiB apart, then disk, then maturity. Method and numbers: [results.md](0001-observability-stack/results.md).

## Decision

- Prometheus stores metrics and runs the alert rules from a file in Git; Alertmanager posts to Discord, repeats and clears alerts, and holds back those that depend on another that is firing; Grafana shows every store from files in Git; Alloy collects.
- VictoriaLogs stores logs and VictoriaTraces stores traces. Loki and Tempo stay one setting away: `LOG_STORE` and `TRACE_STORE` in `stacks/observability/.env.example`, each store's differences in its own folder under `logs/` or `traces/`.
- A read-only gateway (Caddy) is the only way any reader reaches a store: Grafana's data sources, agents, the heartbeat and public stats each log in there and reach only read paths. Every store tested serves writes or admin calls beside its reads, and none has read-only users. Networks keep the rest apart: Alloy is the only writer, and Grafana and public stats reach only the gateway.
- OBI, which traces every program in a container from the kernel (eBPF) without changing it, stays off until its `containers_only` setting works: on its own network it traced the host's programs and the CI VMs too.

Why each store:

- Prometheus over VictoriaMetrics: as whole stacks they tie on workarounds, but VictoriaMetrics needs one more program (vmalert) to run its rules, 7 against 6. VictoriaMetrics is 19 MiB lighter and 1.5 times smaller on disk, gaps the rule does not reach.
- VictoriaLogs over Loki: they tie on must-haves, workarounds and programs and were 43 MiB apart on the live feed, so disk decides: 15.9 B a line against 34.8 B, one reading of a 30-day replay. On that replay VictoriaLogs also searched 2.6 to 8.5 times faster over 7 and 30 days and held a median 363 MiB, where Loki held 1,016 MiB and was killed at a 1 GiB cap.
- VictoriaTraces over Tempo: beside the live stack, Tempo dropped the 555 spans Alloy held and sent late after a 10-minute outage, failing the must-have that every item sent is stored; VictoriaTraces lost none. Tempo also hit its 512 MiB cap every 5 minutes on the normal feed and refuses a search over more than 7 days.
- Out: GreptimeDB (no-data rules never fire), OpenObserve behind Grafana (a reader deleted a log stream through the only plugin with which Grafana reads its logs, an unsigned one-person project), SigNoz and ClickStack (no hold-back between alerts, every rule rewritten, about 21 and 32 workaround points against 5).

## Consequences

- No code of our own runs the alerts: the hold, per-series clears, no-data and the hold-back come from Prometheus and Alertmanager, and the rules are tested with `promtool`.
- Agents read metrics, logs and traces, read-only: in 933 probes through the gateway and Grafana, no write or admin call got through.
- Six programs to keep upgraded instead of four. Grafana is a small image of our own with the VictoriaLogs plugin added by checksum, moved by hand.
- Three `kill -9`s lost 4.9 to 6.8 s of logs each and 19 spans in all, about the stores' 5 s default for saving what they hold in memory; both now save each second, not yet re-measured.
- In a 15-minute burst at about 100 times the log feed, no whole stack (stores, Grafana, rule runner, gateway and Alertmanager) stayed under the 1 GiB it is meant to fit in; the lowest, with this pair, held 1,102 MiB.
- Not yet tested: memory over 7 days, expiry of old data, the alert timers on a real alert, and a post seen in the Discord channel.
- The new stores start empty at the switch; OpenObserve's volume is kept for its 30 days, readable on demand and for a rollback ([switching-observability.md](../runbooks/switching-observability.md)).
