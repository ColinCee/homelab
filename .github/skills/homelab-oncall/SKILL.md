---
name: homelab-oncall
description: "Use when working the homelab's alerts as its on-call agent: the scheduled on-call check on Colin's desktop, or when Colin asks what fired, why, or to fix an alert."
---

# Homelab on-call

Triage every alert that fired since the last check, fix what can be undone, and leave
Colin a short answer he can act on. Alertmanager keeps posting to Discord's #uptime
whether or not this runs, so with the desktop off overnight he still sees each alert and
its "What to do"; this check catches up when the desktop is on. How alerts work:
`docs/architecture.md`, "Monitoring and alerts".

## 1. What fired

Work in a fresh worktree of `D:\Claude Projects\homelab` on `origin/main`.

```bash
# Firing now, with labels and the rule's annotations (title, harm, action, value).
ssh beelink 'docker exec observability-alertmanager-1 amtool alert query --alertmanager.url=http://localhost:9093 -o json'
# What fired in a window, cleared or not: one series per alert and labels.
pw=$(ssh beelink cat code/homelab/secrets/agents_password)
curl -su "agents@homelab.internal:$pw" http://100.100.146.119:5081/prometheus/api/v1/query \
  --data-urlencode 'query=max_over_time(ALERTS{alertstate="firing",alertname!="watchdog"}[12h])'
```

Never print the password. The state file `$LOCALAPPDATA/homelab-oncall/state.json`
holds `last_check` (UTC) and the alerts already worked (alertname, labels, when). Look
back to `last_check`, or 24 h with no file. Skip what it lists unless it fired again
after. Nothing new: write `last_check`, say "Nothing new" in one line, and stop.

## 2. Each alert

1. Read its rule in `stacks/observability/prometheus/rules.yml` and what it measures.
   Query the numbers behind it and the logs of what it names around when it started:
   Prometheus, VictoriaLogs (`/logs/select/logsql/query`) and traces through the same
   gateway, Grafana at `http://grafana.internal`, or `ssh beelink` for `docker` and
   `journalctl --user`.
2. Find the cause, not only the symptom.
3. Fix it when the fix is reversible and the cause is clear: `ssh beelink docker restart
   <container>`, `ssh beelink systemctl --user restart <unit>`. Check the number
   recovered before calling it fixed.
4. A code or config fix goes in a pull request from the worktree, per `AGENTS.md` and
   the `shipping-prs` skill. A change Colin sees, or anything irreversible (deleting
   data, pruning, rebooting, a secret), waits for him: ask in this session and stop.
5. An alert that fired on nothing harmful breaks the alert standard: open a pull
   request that tightens its rule, after replaying it as the architecture doc says.

## 3. Report

In this session, one block per alert: what fired and when, the cause with the query or
log line that shows it, what you did (pull request links), and what is left for Colin,
first if anything is. Then update the state file. If the Discord plugin's tools are
loaded, also post the same in a few lines to #uptime (`1467218691958640720`), mentioning
Colin only when something waits for him.
