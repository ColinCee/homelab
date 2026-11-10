# Observability

Start here when a service is unhealthy. Compose and `config.alloy` under `stacks/observability/` own exact
settings and retention. Alloy collects logs and metrics into OpenObserve ([ADR 0001](adr/0001-openobserve.md)), which
keeps 30 days.

Monitoring on Beelink cannot detect its own complete outage from outside.
Retain an independent external heartbeat when host-outage detection is needed;
do not treat an on-host dashboard as proof of availability.

What watches what:

| Problem | Who warns |
|---|---|
| Beelink fully down | Healthchecks.io, posting to Discord's #uptime |
| Alerting stopped: a rule not run or failing, Alertmanager down, posts not reaching Discord, or the Beelink off | [`heartbeat.yaml`](../.github/workflows/heartbeat.yaml) checks every 15 minutes on the Beelink; [`heartbeat-watch.yaml`](../.github/workflows/heartbeat-watch.yaml), on GitHub's machines, posts to #uptime with a mention when it has not passed for 2 hours |
| Deploy failed | GitHub's email for a failed run; nothing for a deploy stuck in the queue |

GitHub turns off a public repository's scheduled workflows after 60 days with no commits, which would stop both
heartbeat workflows without a post; Renovate's merges keep the repository active.

## What's deployed

| Component | What it does |
|-----------|---------------|
| **Alloy** | Ships metrics and Docker logs to OpenObserve |
| **OpenObserve** | Logs, metrics, alert rules and the [Home dashboard](../stacks/observability/openobserve/dashboards/home.json); `http://openobserve.internal` |
| **Alertmanager** | Posts OpenObserve's alerts to Discord, repeats, clears and holds them back; port 9093 |
| **OpenObserve gateway** | Readers' way in, each with own login and paths ([`Caddyfile`](../stacks/observability/gateway/Caddyfile)); port 5081 |

Home and the rules come from Git on each deploy; edit the files, not the UI (the dashboards skill,
`.github/skills/dashboards/SKILL.md`).

### Dashboard patterns

Use `max by (name)` for container metrics or `max()` for a single host value
where deduplication is needed.

Host metrics (`node_*`) cover the whole machine, Firecracker CI VMs included.
Container metrics do not: the VMs are processes, not containers, so their CPU
and memory come from their slot cgroups (`job="ci-vms"`).

## Logs

Alloy discovers Docker containers through a read-only socket proxy, adds a `container_name`
label, and sends logs to OpenObserve's `containers` stream (`stacks/observability/config.alloy`). It
skips the proxy's and Alloy's lines, and OpenObserve's below WARN; `docker logs` still has them.

To inspect a service, find its current container name and query it in OpenObserve's Logs page:

```bash
docker ps --format '{{.Names}}'
```

```sql
SELECT _timestamp, body FROM containers WHERE container_name = '<current-container-name>'
SELECT _timestamp, body FROM containers WHERE container_name = '<current-container-name>' AND match_all('ERROR')
```

## Metrics

OpenObserve receives metrics, through its Prometheus API, from:

- **Host:** Alloy's Unix exporter (`job="integrations/unix"` — Alloy overrides the configured `job_name`)
- **Containers:** Alloy's cAdvisor exporter (`job="docker"`)
- **CI VMs:** a second cAdvisor exporter limited to the Firecracker slot
  cgroups, `/firecracker/ci-slotN` (`job="ci-vms"`). A slot's cgroup exists
  only while it runs a VM, so counting them counts running VMs, the ones
  waiting ready included. Its relabel keeps only those series, so there is no
  `up{job="ci-vms"}`.
- **Textfiles:** Alloy's textfile collector reads `/var/lib/homelab-metrics/*.prom`: the CI
  supervisor's `homelab_ci_*` (the ci-runner runbook's "Status files"), the SSD's
  `homelab_ssd_*{device="nvme0"}` from `ssd.prom`, the backups' `homelab_backup_*` and
  `homelab_pc_backup_*`, and `homelab_probe_*` from `probes.py`.
- **Alertmanager:** its own metrics (`job="alertmanager"`), such as posts that failed to reach Discord.
- **Flight tracker:** Alloy's blackbox exporter probes the public
  `https://api.colincheung.dev/aircraft` every minute
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

OpenObserve runs the rules in [`alerts.toml`](../stacks/observability/openobserve/alerts.toml) each minute.
`openobserve-apply` applies them on deploy, then runs each rule's query once and fails the deploy on an error, since
OpenObserve sends nothing for a query that fails. A firing rule posts every minute to
[Alertmanager](../stacks/observability/alertmanager/alertmanager.yml) ([ADR 0002](adr/0002-alertmanager.md)), which
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

- An alert fires once its condition has held for the rule's `for`, and clears once it has stayed clear for 10
  minutes; a return within those 10 minutes is the same alert, with no new post. Both are in the rule's query
  (`apply.py`, `query`), and Alertmanager posts the clear 5 minutes after the last firing (`resolve_timeout`). The
  clear post says "Cleared": the number recovered, not that anything was fixed.
- Each post says what broke (title and value), why it matters (`harm`), what to do (`action`), and links a dashboard.
- "Beelink metrics stopped" holds back every other alert while it fires. Only metrics written outside Alloy (the
  textfiles) carry a `no_data` query.
- `tests/test_openobserve_alerts.py` checks each rule against this.

Noise budget: at most one post a day across all rules, over 14 days. Replay before adding or loosening a rule:

```bash
docker exec observability-openobserve-apply-1 python -I /apply/replay.py /apply/alerts.toml 14
```

On 10 November 2026 the rules made 2 posts in the 14 days, which hold metrics from 9 November on.

OpenObserve keeps metric names in lower case. Home Assistant and the `.internal` names are checked by
[`probes.py`](../stacks/observability/probes.py) on the host's network, since the firewall admits only Tailscale and
loopback; its results reach the rules through the textfile collector.

## Common debugging path

1. **A service is unavailable:** check its Compose status, then inspect the
   current container logs in OpenObserve.
2. **Metrics are stale:** check Alloy first (`docker logs observability-alloy-1`), then the
   container metrics in OpenObserve.
