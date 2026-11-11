# 6. A watchdog alert, not GitHub cron, tells when alerting stopped

## Status

Accepted.

## Context

Alerting on the Beelink cannot report its own outage, so something off the Beelink must notice silence. A
15-minute GitHub cron (`heartbeat.yaml`) checked alerting on the Beelink, and an hourly one on GitHub's machines
(`heartbeat-watch.yaml`) posted when it had not passed for 2 hours. On 10–11 Nov 2026 GitHub ran each schedule only
every 3.5 to 8.6 hours, so the watch posted "Alert monitoring stopped" while everything worked. GitHub does not
promise when a scheduled workflow runs. Healthchecks.io was already watching the Beelink, pinged every 5 minutes
by a crontab entry kept outside Git.

## Decision

The Prometheus mixin's standard dead man's switch: an always-firing `watchdog` rule, which Alertmanager sends each
minute to the Healthchecks.io check's ping URL (`HEALTHCHECKS_PING_URL`). The rule stops while Discord refuses posts.
Healthchecks.io posts to #uptime when the pings stop. Both heartbeat workflows and the crontab ping go.

## Consequences

- One check covers the Beelink off, Prometheus not running rules, Alertmanager down and Discord refusing posts; its
  post does not say which.
- A rule failing at run time is covered by "An alert rule is failing", not by the watchdog.
- Healthchecks.io's own settings (period, grace, where it posts) live in its web app, not in Git.
