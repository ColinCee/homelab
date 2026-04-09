# ADR-003: Observability Stack

**Date:** 2026-04-06
**Status:** Accepted

## Context

The homelab had no visibility into host metrics, container health, logs, security events, or network traffic. Dokploy provides basic per-container stats but no historical retention, dashboards, or alerting beyond deploy notifications.

## Decision

Deploy a Grafana-based observability stack ("GPAL") plus CrowdSec for security:

| Component | Purpose |
|-----------|---------|
| **Grafana** | Dashboards, alerting, log exploration |
| **Prometheus** | Metrics storage (PromQL, 30d retention) |
| **Alloy** | Unified collector — host metrics, container metrics (cAdvisor), Docker logs, CrowdSec scraping |
| **Loki** | Log aggregation (LogQL, 30d retention) |
| **CrowdSec** | Collaborative IDS — parses logs, detects attacks, shares threat intel, blocks IPs |
| **Healthchecks.io** | External heartbeat monitoring (free tier) |

## Alternatives Considered

| Alternative | Why Not |
|-------------|---------|
| **Full LGTM** (Loki, Grafana, Tempo, Mimir) | Tempo (tracing) is for microservices; Mimir is for multi-tenant scale. Both overkill. |
| **Netdata** | Beautiful zero-config dashboards, but less customizable than Grafana and no log aggregation. |
| **ELK Stack** (Elasticsearch, Logstash, Kibana) | Resource-heavy (~2-4 GB RAM), complex to operate on a single node. |
| **Uptime Kuma** | Useful for response-time dashboards, but monitoring from the same server can't detect full outages. Healthchecks.io does this externally. |
| **OSSEC / Wazuh** | Full HIDS — 300+ MB RAM, overkill for single node. CrowdSec is lighter and collaborative. |
| **Promtail + Node Exporter** (separate) | Alloy replaces both in a single binary. Fewer containers, simpler config. |

## Consequences

- ~500 MB additional RAM usage (within budget on 15 GB server)
- ~1 GB/month disk for metrics + logs retention (30 day default)
- All config in git (`stacks/observability/`, `stacks/crowdsec/`)
- Grafana accessible at `:3001` via Tailscale only
- Alert rules provisioned as code (YAML), contact point uses env var for Discord webhook
- CrowdSec firewall bouncer actively blocks malicious IPs via iptables
- Healthchecks.io runs as external cron heartbeat — zero server resources
