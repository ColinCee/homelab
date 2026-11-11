# Observability

Start here when a service is unhealthy. Compose and `config.alloy` under `stacks/observability/` own exact
settings and retention. Alloy collects metrics into Prometheus and logs and traces into the log and trace stores, each
keeping 30 days, and Grafana shows them ([ADR 0004](adr/0004-prometheus-grafana.md)). The switch from OpenObserve and
back: [the runbook](runbooks/switching-observability.md).

Monitoring on Beelink cannot detect its own complete outage from outside.
Retain an independent external heartbeat when host-outage detection is needed;
do not treat an on-host dashboard as proof of availability.

What watches what:

| Problem | Who warns |
|---|---|
| Beelink fully down | Healthchecks.io, posting to Discord's #uptime |
| Alerting stopped: a rule not run or failing, Alertmanager down, posts not reaching Discord, or the Beelink off | [`heartbeat.yaml`](../.github/workflows/heartbeat.yaml) checks every 15 minutes on the Beelink; [`heartbeat-watch.yaml`](../.github/workflows/heartbeat-watch.yaml), on GitHub's machines, posts to #uptime with a mention when it has not passed for 2 hours |
| Deploy failed | GitHub's email for a failed run; nothing for a deploy stuck in the queue |
| Someone else on the Beelink: each deploy and CI-host install, a new or unknown Tailscale device, a runner guard that is not main's, a refused job, an SSH login not from the PC or MacBook | The `intrusion` rules, over what [`security_facts.py`](../scripts/security_facts.py) writes each minute |

GitHub turns off a public repository's scheduled workflows after 60 days with no commits, which would stop both
heartbeat workflows without a post; Renovate's merges keep the repository active.

## What's deployed

| Component | What it does |
|-----------|---------------|
| **Alloy** | Ships metrics to Prometheus, Docker logs to the log store, and OTLP traces and logs sent to `alloy:4318` to the trace and log stores; a log line with a `trace_id` opens its trace in Grafana |
| **Prometheus** | Metrics and the [alert rules](../stacks/observability/prometheus/rules.yml) |
| **Log store, trace store** | Loki or VictoriaLogs, Tempo or VictoriaTraces: `LOG_STORE` and `TRACE_STORE` in [`.env.example`](../stacks/observability/.env.example) pick each, one folder per store under `logs/` and `traces/` |
| **Grafana** | The Home dashboard and Explore over every store; `http://grafana.internal`, logged in as `colin` (admin) or `agents` (Viewer, `OPENOBSERVE_AGENTS_PASSWORD`) |
| **Alertmanager** | Posts Prometheus's alerts to Discord, repeats, clears and holds them back |
| **Gateway** | The only way to read the stores, for Grafana, agents and scripts: each login reads only read paths ([`Caddyfile`](../stacks/observability/gateway/Caddyfile)); port 5081 |

Home, the data sources and the rules come from Git on each deploy; Grafana refuses saves from its UI (the
building-dashboards skill, `.github/skills/building-dashboards/SKILL.md`).

Agents read through Grafana's API (or `mcp-grafana`) as `agents`; on the Beelink, also through the gateway:
`curl -u agents@homelab.internal:... http://100.100.146.119:5081/prometheus/api/v1/query?query=up`.

### Dashboard patterns

Use `max by (name)` for container metrics or `max()` for a single host value
where deduplication is needed.

Host metrics (`node_*`) cover the whole machine, Firecracker CI VMs included.
Container metrics do not: the VMs are processes, not containers, so their CPU
and memory come from their slot cgroups (`job="ci-vms"`).

## Logs

Alloy discovers Docker containers through a read-only socket proxy, adds a `container_name`
label, and sends logs over OTLP to the log store (`stacks/observability/config.alloy`), with `container_name` as the
stream, and the host's systemd journal as the container `journal` with each line's `unit`.

To inspect a service, find its current container name (`docker ps --format '{{.Names}}'`) and query it in Grafana's
Explore, on the Logs data source:

```text
{container_name="<name>"} |= "ERROR"                 Loki (LogQL)
container_name:"<name>" ERROR                        VictoriaLogs (LogsQL)
```

## Metrics

Prometheus receives metrics by remote write from the sources below, each scraped every 15 s (the flight-tracker probe
each minute). Alloy sends every metric they expose, so what a later fix needs is
already kept; it drops none by name.

- **Host:** Alloy's Unix exporter (`job="integrations/unix"` — Alloy overrides the configured `job_name`), of the
  root disk only: Alloy runs as its own user with no capabilities and mounts no host folder but what it reads, so it
  can read no host file closed to other users. cAdvisor cannot read containers' own layers, so `container_fs_*` is
  missing.
- **Containers:** Alloy's cAdvisor exporter (`job="docker"`)
- **CI VMs:** a second cAdvisor exporter limited to the Firecracker slot
  cgroups, `/firecracker/ci-slotN` (`job="ci-vms"`). A slot's cgroup exists
  only while it runs a VM, so counting them counts running VMs, the ones
  waiting ready included. Its relabel keeps only those series, so there is no
  `up{job="ci-vms"}`.
- **Textfiles:** Alloy's textfile collector reads `/var/lib/homelab-metrics/*.prom`: the CI
  supervisor's `homelab_ci_*` (the ci-runner runbook's "Status files"), the SSD's
  `homelab_ssd_*{device="nvme0"}` from `ssd.prom`, the backups' `homelab_backup_*` and
  `homelab_pc_backup_*`, `homelab_probe_*` from `probes.py`, and the intrusion rules' facts from `security.prom`.
- **Alertmanager:** its own metrics (`job="alertmanager"`), such as posts that failed to reach Discord.
- **The stores and cloudflared:** their own metrics (`job="logs"`, `"traces"`, `"cloudflared"`), for "A store is
  down" and "A store stopped taking data". Home Assistant, CoreDNS and Caddy run on the host's network, which the
  firewall keeps Alloy from reaching, so their metrics are not collected.
- **Programs over OTLP:** metrics sent to `alloy:4318`; with the `obi` profile on, OBI's request rate, errors and
  duration for every program in a container, named after its executable.
- **Flight tracker:** the `blackbox` container probes the public
  `https://api.colincheung.dev/aircraft` every minute for Prometheus, which scrapes it itself, since it probes any
  address its caller names and Alloy must not reach the internet
  (`job="integrations/blackbox/flight-tracker"`). `probe_success` is 1 only
  when the response says `"apiHealth":"live"`, so tunnel, backend and
  upstream data source failures all show as 0.

Useful checks:

```bash
curl -sf http://beelink.tail4f465e.ts.net:5080/healthz
```

Useful PromQL:

```promql
max by (name) (container_last_seen{job="docker", name!=""})
```

## Alerts

Prometheus runs the rules in [`rules.yml`](../stacks/observability/prometheus/rules.yml) each minute. Rules it cannot
load stop it, which fails the deploy; a rule whose query fails at run time fires "An alert rule is failing". A firing
rule goes to [Alertmanager](../stacks/observability/alertmanager/alertmanager.yml) ([ADR 0002](adr/0002-alertmanager.md)), which
posts to Discord's #uptime through the `DISCORD_PRIVATE_WEBHOOK_URL` webhook. Alertmanager publishes no port, since its API takes
silences without a login; list its alerts with
`docker exec observability-alertmanager-1 amtool alert query --alertmanager.url=http://localhost:9093`.

### The standard

An alert exists only if all three hold; anything else is a dashboard number:

1. Something is harmed now or soon.
2. Colin can act on it.
3. The post says what to do.

| Level | When | Post |
|---|---|---|
| Act now (`now`) | A user-facing service is down, data is being lost, or something is full within 24 h | Mentions Colin, and again every 4 h while it fires |
| This week (`week`) | Something is running out or slipping | No mention, again daily |
| Notice (`notice`) | A deploy or CI-host install, so Colin sees each change he did not make | No mention, once, no "Cleared" |

- An alert fires once its condition has held for the rule's `for`, and clears once it has stayed clear for 10
  minutes; a return within those 10 minutes is the same alert, with no new post: the rule's `for` and
  `keep_firing_for`. The clear post says "Cleared": the number recovered, not that anything was fixed.
- Each post says what broke (title and value), why it matters (`harm`), what to do (`action`), and links a dashboard.
- "Beelink metrics stopped" holds back every other alert while it fires. Only metrics written outside Alloy (the
  textfiles) carry a no-data arm.
- `tests/test_alert_rules.py` checks each rule against this, and runs them with `promtool` over made-up series.

Noise budget: at most one post a day across all rules but notices, over 14 days. Replay before adding or loosening a rule:

```bash
GATEWAY_LOGIN=agents@homelab.internal:<password> uv run python stacks/observability/prometheus/replay.py 14
```

Home Assistant and the `.internal` names are checked by
[`probes.py`](../stacks/observability/probes.py) on the host's network, since the firewall admits only Tailscale and
loopback; its results reach the rules through the textfile collector.

## Common debugging path

1. **A service is unavailable:** check its Compose status, then inspect the
   current container logs in Grafana.
2. **Metrics are stale:** check Alloy first (`docker logs observability-alloy-1`), then the
   container metrics in Grafana.
