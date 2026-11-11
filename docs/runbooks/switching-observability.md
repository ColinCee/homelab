# Switching observability

How the observability stack moves from OpenObserve to Prometheus, Grafana and the picked log and trace stores
([ADR 0004](../adr/0004-prometheus-grafana.md)) in one deploy, and back. It changes the `observability`, `backup` and
`internal-names` stacks together. No new secret: `.env.example` maps the OpenObserve-named ones.

## Before the merge

1. Colin's choice: the trial. The branch's stack runs beside the live one as Compose project `obs-trial`, with
   [`trial.yaml`](../../stacks/observability/trial.yaml) (no host ports, alerts to a local sink, no restarts) under a
   3 GiB `obs-trial.slice`. It shows memory at the real rate, crash and outage loss, and expiry. ADR 0004 is set to
   Accepted only after it, or after Colin decides to skip it. Its stores write to the volumes the live stack will
   use, so before the merge stop it and remove its containers, keeping those volumes:
   `docker compose -p obs-trial ... -f compose.yaml -f trial.yaml down` (no `-v`). The switch then starts on what the
   trial stored.
2. Check the pick in `stacks/observability/.env.example` (`LOG_STORE`, `TRACE_STORE`) matches ADR 0004's. Set its
   status to Accepted and ADR 0001's to "Superseded by 0004", in this pull request.
3. Wait for CI to pass, and for no CI job to run on the Beelink (`gh run list --status in_progress`).
4. On the Beelink, with `am="docker exec observability-alertmanager-1 amtool --alertmanager.url=http://localhost:9093"`:
   - `$am alert query` lists nothing. Alertmanager keeps alerts in memory and is recreated, so an alert firing at the
     switch never gets its "Cleared"; wait until it clears.
   - `$am silence add alertname=beelink-metrics-stopped --duration=30m --author=colin --comment="switch"`: Prometheus
     starts empty, so its first evaluation can fire it before Alloy's first write. Silences are kept on the volume.

## The switch

1. Merge. The deploy job removes `openobserve`, `openobserve-gateway` and `openobserve-apply` (`up --remove-orphans`)
   and starts the new services: port 5080 becomes Grafana and 5081 the new gateway. A heartbeat run before the deploy
   ends fails; heartbeat-watch posts only after 2 hours of that. Note the merge's time: OpenObserve's history is kept
   for 30 days from it.
2. "Unknown container started" has no week of names yet, so a container started by hand in the first week alerts.
3. Check, from a minute after the deploy ends:
   - `http://grafana.internal` asks for a login; as `colin` (the `OPENOBSERVE_ROOT_PASSWORD` secret) Home shows a
     number in every tile and lines in the error panels.
   - `gh workflow run heartbeat.yaml --repo ColinCee/homelab`, then `gh run watch`: it passes and counts 24 rules.
   - `gh workflow run server-health.yaml --repo ColinCee/homelab`: "Firing alerts" is none but the switch's own
     "Deploy started" (the old `update.sh` installs it, silently).
   - Alloy starts on a new volume, so it resends up to 7 hours of the journal and Docker's kept lines.
   - Each intrusion rule posts once to #uptime; after each trigger run `sudo systemctl start homelab-security-facts`:
     `logger -t sshd "Accepted publickey for test from 203.0.113.9 port 1 ssh2"`; `logger -t homelab-runner-guard
     "verdict=refuse GITHUB_REPOSITORY=test/x GITHUB_ACTOR=test"`; re-add the iPhone to the tailnet; and `sudo sh -c
     'echo "#" >> /usr/local/lib/homelab/runner_job_guard.py'` (after 15 minutes; then `sudo
     scripts/install-runner-guard.sh enforce` from the server checkout).
   - `docker ps --filter name=observability-public-stats` shows it healthy within 15 minutes.
   - On the Beelink, since the tailnet policy opens 5081 to no device:
     `curl -su agents@homelab.internal:<password> http://100.100.146.119:5081/prometheus/api/v1/query?query=up`
     answers, and the same login's POST to `.../prometheus/api/v1/write` gets 403. Reading 5081 from the PC needs a
     tailnet policy change, Colin's step (`scripts/tailscale_policy.py`).

## Reading OpenObserve's history

For 30 days after the switch, OpenObserve's volume holds the metrics and logs from before it. On the Beelink:

```bash
cd /home/colin/code/homelab
docker compose --env-file stacks/observability/.env -f stacks/observability/compose.yaml --profile history up -d openobserve-history
```

Then `ssh -L 5082:127.0.0.1:5082 beelink` and open `http://localhost:5082` as `root@homelab.internal`. It expires
nothing while it runs. Stop it with the same command's `stop openobserve-history`.

## Rollback

Revert the merge commit in a pull request and merge it: the deploy brings OpenObserve back on its kept volume and
removes the new services. OpenObserve has no data for the time the new stack ran. The same silence as above keeps
the start-up page away. Then delete the new stores' volumes:

```bash
docker volume rm observability_prometheus-data observability_grafana-data observability_victorialogs-data \
  observability_tempo-data
```

Rollback works until OpenObserve's volume is deleted.

## 30 days after

Colin's step, when nothing has needed a rollback and nothing from before the switch is still wanted:

```bash
docker volume rm observability_openobserve-data observability_alloy-data
docker image rm openobserve/openobserve:v1.1.2
```

Then, in one pull request, delete `openobserve-history` and its volume from `compose.yaml`; the unpicked store's
folder under `stacks/observability/logs/` or `traces/`, its volume name and its cases in the gateway, Alloy and
dashboard tests; the building-dashboards skill's line on keeping both Home files in step; and this runbook with its
pointer in `docs/observability.md`.
