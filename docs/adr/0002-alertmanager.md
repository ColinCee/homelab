# 2. Alertmanager posts OpenObserve's alerts

## Status

Accepted.

## Context

OpenObserve v1.1.2 alerts from the rules in Git ([ADR 0001](0001-openobserve.md)), and Auto Kaizen's Control room, which was to receive and work around its alerts, is shut down. Alone, OpenObserve cannot keep the alert standard ([observability](../observability.md), "Alerts"):

- It holds a resolved message until the repeat interval ends, so a 4-hour reminder means a "Cleared" up to 4 hours late.
- A per-series alert never sends a resolve.
- It cannot hold back the alerts that depend on another.
- `keep_firing_for` never sends a resolve, so a hold against flapping has to live in each query.

## Decision

Prometheus Alertmanager sits between OpenObserve and Discord. Every rule posts each minute it fires to Alertmanager's v2 API with no end time, so Alertmanager ends it 10 minutes (`resolve_timeout`) after the last post. Alertmanager does the per-level repeat, the "Cleared" post, the hold against flapping, the hold-back from "Beelink metrics stopped", and the mention.

Checked on the live instance before building: OpenObserve's template builds Alertmanager's JSON, per-series labels arrive, and each minute's post moves the end time on. Alertmanager's templates have no number formatting, so each rule's query gives display units, rounded.

## Consequences

- One more container (about 30 MB), and the Discord webhook moves from Grafana to it.
- A clear comes 10 to 11 minutes after a rule stops firing, per-series rules included, and an OpenObserve outage shorter than 10 minutes sends nothing.
- An OpenObserve outage over 10 minutes clears every alert, and those still firing post again when it is back; the heartbeat reports the outage.
- An alert that clears while Alertmanager is down gets no "Cleared".
