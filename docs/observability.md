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
| Auto Kaizen's Watcher not working | Grafana's Kaizen's Watcher stopped running |
| Control room down | Grafana's Kaizen Control room unreachable |

## What's deployed

| Component | What it does |
|-----------|---------------|
| **Grafana** | Dashboards, Explore, and alert routing |
| **Prometheus** | Metrics storage for host, containers, CI VMs, Auto Kaizen's Control room and the flight-tracker probe |
| **Loki** | Log storage queried with LogQL |
| **Alloy** | Ships metrics and Docker logs to Prometheus/Loki and OpenObserve |
| **OpenObserve** | Logs and metrics until cutover; `http://openobserve.internal` |

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
  `homelab_ssd_*{device="nvme0"}` from `ssd.prom`, and the backups' `homelab_backup_*` and
  `homelab_pc_backup_*`.
- **Control room (Auto Kaizen):** direct scrape of `http://beelink.tail4f465e.ts.net:8090/metrics`
  (`job="kaizen"`), every series named `kaizen_*`: tasks by state, machine waits, agent
  cost, the GitHub App's and the workflows' shared key's hourly calls, and the signs of
  waste or a stall that the "Kaizen factory" rules read. Those rules carry `route: factory`,
  which the Control room passes to its factory manager. In `kaizen_tasks{state}`,
  `waiting_for_machine` counts waits for GitHub's machines too; `stuck` and
  `kaizen_machine_wait_seconds_max` count homelab machines only. The Control room's code
  is in the private auto-kaizen repo, so list the series with
  `curl -s http://beelink.tail4f465e.ts.net:8090/metrics | grep '^# HELP'`.
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

Grafana alerting is provisioned from
`stacks/observability/provisioning/alerting/` and routes to the `Kaizen` contact
point: every firing and resolved alert, except the few below, goes to the Kaizen Control room's
`/alert`, which opens (or, for a repeat, comments on) an `alert` issue in the
private auto-kaizen repo and closes it on resolve. Each new one starts Auto
Kaizen's responder: it looks at Beelink through the `Remediate` workflow, may
restart one broken container, and opens a Kaizen task for the lasting fix (or to
tune a noisy rule). The Control room keeps one post per alert in Discord's
#alerts forum through the auto-kaizen bot (`KAIZEN_DISCORD_TOKEN` secret): its
state tags, what the responder found, and a line per event; it mentions Colin
only when he must act. The repo is public, so `Remediate` logs states and counts
only; read container logs in Grafana. Grafana and the Control room share the
`KAIZEN_ALERT_SECRET` secret. Test the whole chain with Server health →
`test_alert`.

#uptime (the `Discord Private` webhook) holds only what must arrive while the Beelink or the
Control room may be down: Kaizen Control room unreachable and Healthchecks.io's Beelink up/down.
A `route: colin` rule (only Colin can act: Unknown container started, Beelink SSD wearing out)
starts no responder; its #alerts post asks him its `needs` annotation, mentioning him once.
Every other alert, no-data and evaluation errors included, gets only the Control room's one
post. A deploy that changes these files recreates Grafana,
which reloads them; they are not polled.

Grafana's root URL uses the Tailscale IP, not the bare `beelink` hostname: Discord rejects a
host-only URL in the alert embed with HTTP 400.

Each rule in [`rules.yaml`](../stacks/observability/provisioning/alerting/rules.yaml) carries its
own description and the action to take, which reach the alert's post; read the rule, not a list here.
Rules on the CI VMs (swapping, slow disk, out of memory, stuck waiting) point at the ci-runner
runbook's "Why a job waits".

Host RAM uses `node_memory_MemAvailable_bytes`, not `node_memory_AvailableBytes`. Check the rule's
query when a dashboard has data but no alert.

OpenObserve's copy of each rule, in [`alerts.toml`](../stacks/observability/openobserve/alerts.toml), is
applied on deploy by `openobserve-apply` and posts only to the Control room's shadow path until the cutover
(`live`). It keeps metric names in lower case, selects no series by a label it lacks, and repeats a
per-series alert's firing each minute without a resolve, so stopped firings mean cleared.

The #uptime webhook and the Control room's `/alert` both use the provisioned
`homelab-discord` notification template.
No-data and evaluation errors are separate alerts, grouped by affected rule;
they mean monitoring is unavailable, not that a threshold was crossed.
A resolved message describes an ended alert, not proof of recovery.

## Common debugging path

1. **A service is unavailable:** check its Compose status, then inspect the
   current container logs in Loki.
2. **Metrics are stale:** check Alloy first, then Prometheus targets and the
   container metrics in Grafana.
