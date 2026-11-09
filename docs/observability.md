# Observability

Start here when a service is unhealthy. Compose, `config.alloy`, and Grafana
provisioning under `stacks/observability/` own exact settings and retention.
Alloy collects both logs and metrics to avoid separate collectors.
OpenObserve is to replace Grafana, Prometheus and Loki
([ADR 0001](adr/0001-openobserve.md)).

Monitoring on Beelink cannot detect its own complete outage from outside.
Retain an independent external heartbeat when host-outage detection is needed;
do not treat an on-host dashboard as proof of availability.

What watches what:

| Problem | Who warns |
|---|---|
| Beelink fully down | Healthchecks.io, posting to Discord's #uptime |
| Deploy failed | `deploy.yaml` stops Auto Kaizen's homelab line |
| Deploy queued over 15 minutes or failed before its first step | Auto Kaizen's Watcher stops the homelab line |

## What's deployed

| Component | What it does |
|-----------|---------------|
| **Grafana** | Dashboards, Explore, and alert routing |
| **Prometheus** | Metrics storage for host, containers, CI VMs and the flight-tracker probe |
| **Loki** | Log storage queried with LogQL |
| **Alloy** | Ships metrics and Docker logs to Prometheus/Loki and OpenObserve |
| **OpenObserve** | Logs and metrics until cutover; `http://openobserve.internal` |
| **Alertmanager** | Posts OpenObserve's alerts to Discord, repeats, clears and holds them back; port 9093 |
| **OpenObserve gateway** | Readers' way in, each with own login and paths ([`Caddyfile`](../stacks/observability/gateway/Caddyfile)); port 5081 |

Grafana provisions dashboards directly from the read-only mounted JSON files in
`stacks/observability/dashboards/`. Edit those files and deploy; Grafana polls for
updates. UI edits are disabled so Git remains the source of truth.

There is one dashboard, [Home](../stacks/observability/dashboards/home.json),
at the top level, and Grafana opens on it for everyone (`GF_USERS_HOME_PAGE`).
Add panels to it rather than adding dashboards. From the top:

- **Top strip:** any alert not OK; what needs you (stuck Kaizen tasks, held
  changes, backup age, flight tracker); the Beelink's CPU, memory, disk, CI VMs
  and uptime
- **Beelink:** CPU and memory over time, split into Docker containers,
  Firecracker CI VMs and the rest
- **Kaizen:** working, waiting, merged and cost, tasks by state, the
  longest wait for a machine, the GitHub App's calls used against its hourly limit, and below
  it the Control room's minutes since start, so a climb in calls after a restart shows
- **Containers:** a bar per container for CPU and for memory
- **Backups:** one line of numbers
- **Collapsed sections**, which load only when opened: alerts, containers,
  CI VMs, network and backups over time, with their logs

The alert rules live in Grafana's "Alert rules" folder, which holds no dashboards.
To remove a rule, also list its uid under `deleteRules` in `rules.yaml` (a contact
point: `deleteContactPoints` in `contactpoints.yaml`): Grafana keeps one that is only
deleted from the file.
Grafana gives a top-level provisioned dashboard no viewer access, so the
deploy workflow grants the Viewer role view access to Home after each deploy of
the observability stack.

Grafana allows anonymous read-only (Viewer) access so dashboards open without
a login, including for agents checking a change. Editing, Explore and
administration still require the admin password. This relies on the port
being bound to the Tailscale address, on the tailnet policy limiting who
can reach it, and on `stacks/internal-names` refusing Beelink's own address; remove `GF_AUTH_ANONYMOUS_*` if either changes.

### Dashboard patterns

Use `max by (name)` for container metrics or `max()` for a single host value
where deduplication is needed. Data before November 2026 carries a different
`instance` label for each Alloy container; Alloy now runs with
`hostname: beelink`, so new series keep one.

Host metrics (`node_*`) cover the whole machine, Firecracker CI VMs included.
Container metrics do not: the VMs are processes, not containers, so their CPU
and memory come from their slot cgroups (`job="ci-vms"`).

Datasource UIDs are pinned to `prometheus` and `loki` in
`provisioning/datasources/datasources.yaml`. Grafana does not update
UIDs on existing datasources via provisioning. If they drift, inspect the
provisioned and live datasource identities before making changes; do not
blindly edit Grafana's database.

## Logs: Loki via Alloy

Alloy discovers Docker containers through a read-only socket proxy, adds a `container_name`
label, and forwards logs to Loki (`stacks/observability/config.alloy`).

To inspect a service, find its current container name and query it in Grafana
Explore:

```bash
docker ps --format '{{.Names}}'
```

```logql
{container_name="<current-container-name>"}
{container_name="<current-container-name>"} |= "ERROR"
```

## Metrics: Prometheus

Prometheus receives metrics from:

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
curl -sf http://beelink.tail4f465e.ts.net:3001/api/health
```

Useful PromQL:

```promql
max by (name) (container_last_seen{job="docker", name!=""})
```

## Alerts

OpenObserve runs the rules in [`alerts.toml`](../stacks/observability/openobserve/alerts.toml) each minute.
`openobserve-apply` applies them on deploy, then runs each rule's query once and fails the deploy on an error, since
OpenObserve sends nothing for a query that fails. A firing rule posts every minute to
[Alertmanager](../stacks/observability/alertmanager/alertmanager.yml) ([ADR 0002](adr/0002-alertmanager.md)), whose
`shadow` receiver holds alerts and posts nothing until the switch from Grafana; Grafana's rules in
`provisioning/alerting/` still post to Discord's #uptime. Alertmanager publishes no port, since its API takes
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

Noise budget: at most one post a day across all rules, over 14 days. Replay before adding or loosening a rule
(Prometheus holds the longer history until OpenObserve has 14 days; without the variable it reads OpenObserve):

```bash
docker exec -e REPLAY_PROMETHEUS=http://prometheus:9090 observability-openobserve-apply-1   python -I /apply/replay.py /apply/alerts.toml 14
```

On 9 November 2026 the rules made 4 posts in 14 days.

OpenObserve keeps metric names in lower case. Home Assistant and the `.internal` names are checked by
[`probes.py`](../stacks/observability/probes.py) on the host's network, since the firewall admits only Tailscale and
loopback; its results reach the rules through the textfile collector.

## Common debugging path

1. **A service is unavailable:** check its Compose status, then inspect the
   current container logs in Loki.
2. **Metrics are stale:** check Alloy first, then Prometheus targets and the
   container metrics in Grafana.
