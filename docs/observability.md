# Observability

Start here when a service is unhealthy. Compose, `config.alloy`, and Grafana
provisioning under `stacks/observability/` own exact settings and retention.
Alloy collects both logs and metrics to avoid separate collectors. Tracing and
multi-tenant metrics infrastructure are unnecessary for this single host.

Monitoring on Beelink cannot detect its own complete outage from outside.
Retain an independent external heartbeat when host-outage detection is needed;
do not treat an on-host dashboard as proof of availability.

The [deploy watchdog](../.github/workflows/deploy-watchdog.yaml) runs on
GitHub-hosted runners when a deploy finishes, 16 minutes after one is queued,
and every 15 minutes as a backup (GitHub delays schedules on quiet repos). It
posts to Discord when a `deploy.yaml` run has been queued for over 15 minutes,
which usually means the `beelink` runner is down, and stops Auto Kaizen's line
when the last deploy failed or was blocked by the runner guard. It posts once when a problem starts and once when it clears, using its
previous run's conclusion as state. Healthchecks.io remains the external
heartbeat for full host outages.

The [Kaizen watchdog](../.github/workflows/kaizen-watchdog.yaml) runs there every 15 minutes too, and posts
once when Auto Kaizen's Watcher (due every 20 minutes) has not worked for an hour, and once when it works again.
The Grafana rule Kaizen's Watcher stopped running checks the same from the Beelink, so one of the two warns whether GitHub
stops starting Kaizen's jobs or the Beelink goes down.

## What's deployed

| Component | What it does |
|-----------|---------------|
| **Grafana** | Dashboards, Explore, and alert routing |
| **Prometheus** | Metrics storage for host, containers, CI VMs, Auto Kaizen's Control room and the flight-tracker probe |
| **Loki** | Log storage queried with LogQL |
| **Alloy** | Scrapes metrics and ships Docker logs into Prometheus/Loki |

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
  longest wait for a machine, and the GitHub App's calls used against its hourly limit
- **Containers:** a bar per container for CPU and for memory
- **Backups:** one line of numbers
- **Collapsed sections**, which load only when opened: alerts, containers,
  CI VMs, network and backups over time, with their logs

The alert rules live in Grafana's "Alert rules" folder, which holds no dashboards.
Grafana gives a top-level provisioned dashboard no viewer access, so the
deploy workflow grants the Viewer role view access to Home after each deploy of
the observability stack.

Grafana allows anonymous read-only (Viewer) access so dashboards open without
a login, including for agents checking a change. Editing, Explore and
administration still require the admin password. This relies on the port
being bound to the Tailscale address and on the tailnet policy limiting who
can reach it; remove `GF_AUTH_ANONYMOUS_*` if either changes.

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
  `up{job="ci-vms"}`. What each job needed inside its VM, and its out-of-memory kills, come
  through the host's textfile collector (`homelab_ci_vm*`), as does the longest-waiting queued job's
  minutes per reason (`homelab_ci_wait_seconds{kind}`); see the ci-runner runbook. The host SSD's
  lifetime bytes written and read, wear and spare (`homelab_ssd_*{device="nvme0"}`) come the same way,
  from `ssd.prom`.
- **Control room (Auto Kaizen):** direct scrape of `http://beelink.tail4f465e.ts.net:8090/metrics`
  (`job="kaizen"`): `kaizen_tasks{state}` (working, waiting_for_machine,
  stuck, needs_colin, merged_today), `kaizen_machine_wait_seconds_max`,
  `kaizen_agent_cost_usd_today`, `kaizen_last_merge_timestamp_seconds`, and
  `kaizen_tasks_read_timestamp_seconds` (its last good read of GitHub), and the
  Kaizen GitHub App's hourly calls (`kaizen_github_app_calls_used`, `_limit`,
  `_reset_timestamp_seconds`), the workflows' shared key's
  (`kaizen_github_workflow_key_calls_remaining`, `_limit`, `_reset_timestamp_seconds`,
  `_read_timestamp_seconds`), and signs of waste or a stall that the "Kaizen factory"
  rules read: `kaizen_workflow_failures_last_hour{cause}`,
  `kaizen_goal_owner_runs_since_change{goal}`, `kaizen_stopped_tasks_alike{reason}`,
  `kaizen_silent_tasks`, `kaizen_agent_cost_usd_last_hour` and
  `kaizen_anomaly_read_timestamp_seconds`. Those rules carry `route: factory`, which the
  Kaizen payload passes on, so the Control room files them for its factory manager.
  `waiting_for_machine` counts waits for GitHub's machines too; `stuck` and the
  longest wait count homelab machines only. The Control room's code lives in the
  private auto-kaizen repo.
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
point: every firing and resolved alert goes to the Kaizen Control room's
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

The Control room cannot post its own outage, so Kaizen Control room unreachable also goes
straight to the `Discord Private` webhook (Discord's #uptime channel), as do the deploy
watchdog's stuck-deploy posts and Healthchecks.io's Beelink up/down. Every other alert,
no-data and evaluation-error alerts included, gets only the Control room's one post. A failed
deploy stops Auto Kaizen.
After deploying alerting file changes, reload them with Grafana's authenticated
`POST /api/admin/provisioning/alerting/reload` API; unlike dashboards, these files
are not polled. A Grafana-only restart also reloads them.

Grafana's root URL uses the Tailscale IP rather than the bare `beelink`
hostname. Discord validates the URL in Grafana's alert embed and rejects
host-only URLs with HTTP 400.

The shipped rules cover host-level pressure such as:

- high CPU
- high RAM
- high disk usage
- **Beelink swapping, slowing Kaizen's VMs**: over 1 MB/s swapped out for 30 minutes; swap
  shares the SSD with every CI VM's disk
- **Beelink disk too slow for Kaizen's VMs**: tasks waited on the disk over 60% of the time for
  30 minutes; over half holds new CI VMs back
- **A Kaizen job ran out of memory**: a CI VM's kernel killed a process for lack of memory
- **Kaizen jobs need nearly all their VM's memory**: over a quarter of one kind's job VMs
  needed over 80% of their memory in 6 hours (at least 8 VMs)
- **Flight Tracker site down**: the live-data probe has failed for 10 minutes
- **Unknown container started**: a container whose name was not seen in the week
  before has run for 15 minutes, as one planted by an intruder would; redeploys
  keep their names and quick `docker run` checks stop sooner, so neither fires
- **New Tailscale device**: a device visible to the Beelink was added to the
  tailnet after the approved-before time in the rule, as an intruder's would be.
  It stays on until the device is removed or approved by moving that time past
  it. A 5-minute timer records each device's added time, and **Tailscale device
  check stopped** fires when it has not for 15 minutes. Policy file changes are
  not covered
- **Beelink SSD wearing out**: the SSD reports over 80% of its rated write endurance used.
  A 15-minute timer records its wear and lifetime writes, and **SSD wear check stopped**
  fires when it has not for an hour
- **PC backup stopped arriving**: no file from the Windows PC in 50 hours
- **Kaizen tasks stuck waiting for a test VM**: a Kaizen task has waited over 20 minutes
  for a homelab CI machine; its text gives the minutes per reason (see the ci-runner runbook's
  "Why a job waits")
- **Kaizen hasn't merged anything in 3 hours**: Kaizen tasks have been running or waiting for 3
  hours and nothing has merged in that time
- **Kaizen's GitHub App running out of calls**: the App has used over 80% of its hourly
  GitHub calls with over 15 minutes until they reset; at the limit every Builder fails until
  then. In the hour's last 15 minutes it stays quiet, as the calls come back soon
- **Kaizen Control room unreachable**: Prometheus has not reached the Control room for
  5 minutes, so alerts are not reaching the #alerts forum

Host RAM uses `node_memory_MemAvailable_bytes`; `node_memory_AvailableBytes`
does not exist in Alloy's Unix exporter. Verify the rule's exact query in
Prometheus when a dashboard has data but an alert does not.

The #uptime webhook and the Control room's `/alert` both use the provisioned
`homelab-discord` notification template. Threshold alerts show whatever the rule's `value` annotation holds and the
threshold duration.
No-data and evaluation errors remain separate alerts, grouped by affected rule;
they mean monitoring is unavailable, not that a resource threshold was crossed.
Resolved messages describe an ended alert, not proof of recovery. Check the
current evaluation before declaring the resource healthy. Tests are explicitly
labelled and do not represent resource incidents.

## Common debugging path

1. **A service is unavailable:** check its Compose status, then inspect the
   current container logs in Loki.
2. **Metrics are stale:** check Alloy first, then Prometheus targets and the
   container metrics in Grafana.
