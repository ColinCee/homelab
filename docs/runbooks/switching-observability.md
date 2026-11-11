# Switching observability

How the observability stack moves from OpenObserve to Prometheus, Grafana and the picked log and trace stores
([ADR 0003](../adr/0003-prometheus-grafana.md)) in one deploy, and back. It changes the `observability`, `backup` and
`internal-names` stacks together; no secret changes, since `.env.example` maps the OpenObserve-named secrets.

## Before the merge

1. Set the pick in `stacks/observability/.env.example` (`LOG_STORE`, `TRACE_STORE`), fill in ADR 0003's pick, set its
   status to Accepted and ADR 0001's to "Superseded by 0003", in the same pull request.
2. Wait for CI to pass, and for no CI job to be running on the Beelink (`gh run list --status in_progress`).
3. Note what is firing now, to compare after: Alertmanager's list,
   `docker exec observability-alertmanager-1 amtool alert query --alertmanager.url=http://localhost:9093`.

## The switch

1. Merge. The deploy job runs `up --remove-orphans`, which removes `openobserve`, `openobserve-gateway` and
   `openobserve-apply` and starts the new services; port 5080 becomes Grafana and 5081 the new gateway. The
   `observability_openobserve-data` volume stays.
2. Expect one "Beelink metrics stopped" post and its "Cleared" about 10 minutes later: Prometheus starts empty and
   its first evaluation can run before Alloy's first write. An alert firing before the switch posts "Cleared" 5
   minutes after OpenObserve stops, then fires again once Prometheus has waited out its `for`. "Unknown container
   started" has no week of names yet, so a container started by hand in the first week alerts even if it ran before.
3. Check, in this order:
   - `http://grafana.internal` opens Home without a login, every tile shows a number, and the error panels show
     lines.
   - `gh workflow run heartbeat.yaml --repo ColinCee/homelab`, then `gh run watch`: it passes and counts 15 rules.
   - `gh workflow run server-health.yaml --repo ColinCee/homelab`: "Firing alerts" matches the list from step 3
     above.
   - `docker ps --filter name=observability-public-stats` shows it healthy within 15 minutes.
   - `curl -su agents@homelab.internal:<password> http://beelink.tail4f465e.ts.net:5081/prometheus/api/v1/query?query=up`
     answers, and a write such as `curl -X POST .../prometheus/api/v1/write` gets 403.

## Rollback

Revert the merge commit in a pull request and merge it: the deploy brings OpenObserve back on its kept volume and
removes the new services, whose volumes stay. OpenObserve has no data for the time the new stack ran, and Grafana's
data is unreadable until a switch again. The first deploy back may post "Beelink metrics stopped" for the same reason
as above.

## A week after

When nothing has needed a rollback:

```bash
docker volume rm observability_openobserve-data
docker image rm openobserve/openobserve:v1.1.2
```

Then delete the unpicked store's folder under `stacks/observability/logs/` or `traces/`, its volume name in
`compose.yaml`, and its rows in the gateway and Alloy tests.
