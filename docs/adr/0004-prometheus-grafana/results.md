# Side-by-side test of six stacks

Run on the Beelink on 10 November 2026 for about 50 minutes, beside the live stack and without touching it.

## Method

- One throwaway Alloy ran the live `config.alloy` at a 15 s scrape with no drop rules and wrote to every store at once, each on its own queue: remote write for metrics, OTLP for logs and traces. About 587 readings a second reached each store.
- Every store ran with its maker's small single-node settings, under one memory cap, with no host ports.
- The 13 live rules were ported mechanically (the hold to `keep_firing_for`, the no-data query to `or on() (...) * -1`, metric names to their real case), plus scaled-down test rules for each alert behaviour. Each rule runner posted to its own copy of the live Alertmanager, which posted to a local sink.
- Readers' writes were probed through Grafana with a Viewer token, or through the gateway for OpenObserve.
- Three fresh agents per stack answered a metric, a log and an alert question with only the reader login; the answers were checked against `/proc/meminfo`, `docker logs` and each Alertmanager's own state.

## Must-haves

| Must | OpenObserve today | OpenObserve + vmalert + Grafana | Prom + Loki | Prom + VictoriaLogs | VictoriaMetrics + VL + VT | GreptimeDB |
|---|---|---|---|---|---|---|
| Every source arrives | pass | pass | pass | pass | pass | pass |
| Log lines sent = stored | pass | pass | pass | pass | pass | pass |
| Alert behaviours | pass, with workarounds | pass | pass | pass | pass | fail: no-data never fires |
| A failed query is noticed | workaround | pass | pass | pass | pass | pass |
| From Git, UI edits refused | workaround | pass | pass | pass | pass | pass |
| Reader writes refused | via the gateway | fail: a Viewer deleted a stream | fail: a Viewer's GET stopped Loki | fail: a Viewer wrote lines | fail: writes to both stores | pass |
| Agents answer (3 questions, 3 agents) | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |

A front service that passes only read paths turns each "Reader writes refused" fail into a pass, at 3 workaround points.

## Numbers

| | OpenObserve today | OpenObserve + vmalert + Grafana | Prom + Loki | Prom + VictoriaLogs | VictoriaMetrics + VL + VT | GreptimeDB |
|---|---|---|---|---|---|---|
| Workaround points | 12 | 4 | 6 | 5 | 5 | 9-12 |
| Programs, traces included | 4 | 5 | 6 | 6 | 7 | 4 |
| Memory, sum of medians, three 20-minute windows (MiB) | 337 / 344 / 347 | 584 / 592 / 599 | 387 / 398 / 403 | 345 / 355 / 362 | 369 / 379 / 381 | 392 / 430 / 429 |
| Bytes per metric reading, a full day (earlier run) | 12.4 | 12.4 | 2.4 | 2.4 | about 1 (maker, not measured) | none published |
| Home metric queries, median (ms) | 18 | 24 | 1.6 | 1.6 | 1.4 | 20 |
| Log panels, median (ms) | 21 | 36 | 11.5 | 3.2 | 3.2 | 19 |
| Clean hold-back, of 3 copies | 1 | 1 | 3 | 3 | 1 | 0 |

## Log and trace stores

Run on 11 November 2026, 00:07-02:05 UTC, in its own Compose project beside the live stack. A replay of two days of the live log export, shifted back to fill 30 days (full volume for 24 h and 7 d, 48% of the planned volume for 30 d), went to both log stores through one Alloy; synthetic traces went to both trace stores at about five times the estimated real span rate. Three interleaved rounds; the answer key is a grep of the replay file, and every answer matched it.

| Logs | Loki 3.7.9 | VictoriaLogs 1.53.0 |
|---|---|---|
| 24 h substring search, newest 100, cold (s) | 2.22 | 0.35 |
| 7 d search / count (s) | 18.3 / 24.8 | 2.21 / 3.74 |
| 30 d search / count (s) | 15.1 / 67.0 | 5.88 / 7.90 |
| Memory during the backfill and queries, median / peak (MiB) | 1,016 / 2,004, killed at a 1 GiB cap | 363 / 650 under 1 GiB |
| Disk per line | 34.8 B | 15.9 B |
| Lines stored of 115,234,477 sent | 2,740 short, cause unproven | all |
| Viewer writes through Grafana, behind a front | all refused | all refused |

The trace stores ran again at 03:28-04:23 UTC at ten times the estimated real span rate (150 spans a second for 20 minutes).

| Traces | Tempo 3.1.1 | VictoriaTraces 0.13.0 |
|---|---|---|
| Spans stored of 180,000 sent | all | all |
| Find by trace ID (s) | 0.022-0.054 | 0.009-0.013 |
| Search, limit 20 (s) | 0.011-0.016 | 0.067-0.092 (its experimental Tempo API) |
| Memory during the load, median / peak (MiB) | 173 / 316 | 71 / 93 |
| Memory peak over the whole run, searches over every trace included (MiB) | 734 | 106 |
| Disk per span | about 500 B | about 97 B |
| Viewer through Grafana, stock | read the config; a POST changed a partition's state | every probe refused |
| Viewer through Grafana, with `http_api_prefix: /tempo` | only the query API answered | |
| Finished release | yes | no |

## On the live feed

Run on 11 November 2026, 04:34-08:36 UTC: one test Alloy with the live scrapes and log pipeline, fanned out to two stores per layer, each alone under its own cap; traces at the estimated real rate of 15 spans a second; searches in 5 rounds, store order alternating.

| | Tempo 3.1.1, 512 MiB cap | VictoriaTraces 0.13.0, 256 MiB cap |
|---|---|---|
| Spans stored of 206,472 sent | all | all |
| Memory peak, searches included (MiB) | 361, no cap hit | 84, no cap hit |
| Disk per span | 85 B in its compacted blocks, plus a rolling 43-72 MB of replaced ones | 79 B |
| Viewer held to the query API | yes, with `http_api_prefix: /tempo` | yes |

Prometheus stored every sample Alloy sent at 2.83 B a reading in its first 2-hour block, at most 93 MiB; VictoriaMetrics took 1.90 B and 74 MiB, gaps the pick rule does not reach. Loki's earlier shortfall was its own deduplication of identical lines, so both log stores pass; the live feed carried too few lines to re-measure disk per line. Grafana under a 320 MiB cap hit it often at 311 MiB, so it runs under 512 MiB.

## Not yet tested

Memory over 7 days, a catch-up after an outage, crash and outage loss, expiry, and the real alert timers with a real Discord post. The 7-day run covers them before this record is accepted.
