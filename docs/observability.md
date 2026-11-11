# Observability

Start here when a service is unhealthy. Compose and `config.alloy` under `stacks/observability/` own exact
settings and retention. Alloy collects metrics into Prometheus and logs and traces into the log and trace stores, each
keeping 30 days, and Grafana shows them ([ADR 0001](adr/0001-observability-stack.md)).

Monitoring on the Beelink cannot see its own outage, so what watches what:

| Problem | Who warns |
|---|---|
| The Beelink off, or alerting stopped: Prometheus or Alertmanager down, or Discord refusing a post in the last hour | The always-firing `watchdog` rule, which Alertmanager sends to Healthchecks.io each minute; Healthchecks.io posts to #uptime when it stops ([ADR 0006](adr/0006-watchdog-not-github-cron.md)) |
| A rule failing | "An alert rule is failing" |
| Deploy failed | GitHub's email for a failed run; nothing for a deploy stuck in the queue |
| Someone else on the Beelink: a deploy or CI-host install not by Colin or Renovate, a new or unknown Tailscale device, a runner guard that is not main's, a refused job, an SSH login not from a device the checks found on the tailnet at their first run | The `intrusion` rules, over what [`security_facts.py`](../scripts/security_facts.py) writes each minute |

## What's deployed

| Component | What it does |
|-----------|---------------|
| **Alloy** | Ships metrics to Prometheus, Docker logs to the log store, and OTLP traces and logs sent to `alloy:4318` to the trace and log stores; a log line with a `trace_id` opens its trace in Grafana |
| **Prometheus** | Metrics and the [alert rules](../stacks/observability/prometheus/rules.yml) |
| **Log store, trace store** | Loki or VictoriaLogs, Tempo or VictoriaTraces: `LOG_STORE` and `TRACE_STORE` in [`settings.env`](../stacks/observability/settings.env) pick each, one folder per store under `logs/` and `traces/`; a swap starts the other store empty |
| **Grafana** | The Home dashboard and Explore over every store; `http://grafana.internal`, logged in as `colin` (admin) or `agents` (Viewer, `OPENOBSERVE_AGENTS_PASSWORD`) |
| **Alertmanager** | Posts Prometheus's alerts to Discord, repeats, clears and holds them back |
| **Gateway** | The only way to read the stores, for Grafana, agents and scripts: each login reads only read paths ([`Caddyfile`](../stacks/observability/gateway/Caddyfile)); port 5081 |

Home, the data sources and the rules come from Git on each deploy; Grafana refuses saves from its UI (the
building-dashboards skill, `.github/skills/building-dashboards/SKILL.md`).

Agents read through Grafana's API (or `mcp-grafana`) as `agents`; on the Beelink, also through the gateway:
`curl -u agents@homelab.internal:... http://100.100.146.119:5081/prometheus/api/v1/query?query=up`.

Dashboards and their queries: the building-dashboards skill.

## Traps

- Logs: Alloy sends every container's lines and the host's journal (as the container `journal`, with each line's
  `unit`, without sudo's and tailscaled's) to the log store, stream `container_name`
  ([`config.alloy`](../stacks/observability/config.alloy)). `loki.secretfilter` replaces each secret it recognises
  with `REDACTED`; a secret in a form its rules miss still reaches the store.
- Host metrics carry `job="integrations/unix"`: Alloy overrides the configured `job_name`. They cover the root disk
  only, and per-container disk space (`container_fs_*`) is missing, since cAdvisor cannot read containers' layers.
- Host metrics (`node_*`) include the CI VMs; container metrics do not. The VMs' CPU and memory come from their slot
  cgroups (`job="ci-vms"`); a slot's cgroup exists only while it runs a VM, so counting them counts running VMs, and
  there is no `up{job="ci-vms"}`.
- Home Assistant, CoreDNS and Caddy run on the host's network, which the firewall keeps Alloy from reaching, so their
  metrics are not collected; [`probes.py`](../stacks/observability/probes.py) checks them from the host's network.
- The flight tracker's `probe_success` is 1 only when its public API says `"apiHealth":"live"`, so tunnel, backend
  and upstream failures all show as 0. Prometheus scrapes `blackbox` itself, since Alloy must not reach the internet.
- Health of Grafana from the tailnet: `curl -sf http://beelink.tail4f465e.ts.net:5080/healthz`.
- Textfile metrics (`/var/lib/homelab-metrics/*.prom`) come from the CI supervisor, the SSD, the backups, the probes
  and the intrusion facts.

## Alerts

Prometheus runs the rules in [`rules.yml`](../stacks/observability/prometheus/rules.yml) each minute. Rules it cannot
load stop it, which fails the deploy; a rule whose query fails at run time fires "An alert rule is failing". A firing
rule goes to [Alertmanager](../stacks/observability/alertmanager/alertmanager.yml) ([ADR 0001](adr/0001-observability-stack.md)), which
posts to Discord's #uptime through the `DISCORD_PRIVATE_WEBHOOK_URL` webhook, and pings Healthchecks.io through `HEALTHCHECKS_PING_URL`. Alertmanager publishes no port, since its API takes
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
  minutes; a return within those 10 minutes is the same alert, with no new post: the rule's `for` and
  `keep_firing_for`. The clear post says "Cleared": the number recovered, not that anything was fixed.
- Each post says what broke (title and value), why it matters (`harm`), what to do (`action`), and links a dashboard.
- "Beelink metrics stopped" holds back every alert that reads Alloy's metrics while it fires. Only metrics written outside Alloy (the
  textfiles) carry a no-data arm.
- `tests/test_alert_rules.py` checks each rule against this, and runs them with `promtool` over made-up series.

Noise budget: at most one post a day across all rules, over 14 days. Test a rule with promtool, never by writing fake
events on the Beelink: those post to #uptime as real ones. Replay before adding or loosening a rule:

```bash
GATEWAY_LOGIN=agents@homelab.internal:<password> uv run python stacks/observability/prometheus/replay.py 14
```

## Common debugging path

1. **A service is unavailable:** check its Compose status, then inspect the
   current container logs in Grafana.
2. **Metrics are stale:** check Alloy first (`docker logs observability-alloy-1`), then the
   container metrics in Grafana.
