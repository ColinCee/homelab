"""Push a few read-only numbers about this server to the blog's home page.

Runs in this stack's public-stats container. Every five minutes
it asks Prometheus and the already-public flight-tracker API for a handful of
numbers and PUTs them to the blog, which shows them in its home-lab figure.
The body is built from named numbers only, never by forwarding a response, so
nothing else about the host can leak. See docs/public-stats.md.
"""

import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# The stacks in this repository; tests/test_public_stats.py keeps it in step.
STACKS = ("backup", "crowdsec", "flight-tracker", "home-assistant", "observability")
EVERY_S = 300
# After a failure, sooner: the first push decides whether a deploy's
# readiness wait sees the container healthy.
RETRY_S = 15
TIMEOUT_S = 15
# Cloudflare refuses the default Python-urllib user agent.
USER_AGENT = "homelab-public-stats/1"
HEARTBEAT = Path("/tmp/ready")

QUERIES = {
    # Stacks with a container seen running in the last minute.
    "running": "count by (container_label_com_docker_compose_project) "
    '(time() - container_last_seen{container_label_com_docker_compose_project!=""} < 60)',
    "blocked": "sum(cs_active_decisions)",
    "ha_started": 'max(container_start_time_seconds{name="homeassistant"})',
    "backup_at": "max(homelab_backup_last_success_timestamp_seconds)",
}


class StatsError(Exception):
    pass


def get_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return json.load(response)


def query(prometheus: str, promql: str) -> list[dict[str, Any]]:
    url = f"{prometheus}/api/v1/query?" + urllib.parse.urlencode({"query": promql})
    body = get_json(url)
    if body.get("status") != "success":
        raise StatsError(f"prometheus: {body.get('error', 'query failed')}")
    return body["data"]["result"]


def scalar(result: list[dict[str, Any]]) -> float | None:
    return float(result[0]["value"][1]) if result else None


def whole(value: object) -> int | None:
    """A count the blog accepts, or None for anything else (null, NaN, +Inf)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value) if math.isfinite(value) and value >= 0 else None


def build_body(
    results: dict[str, list[dict[str, Any]]], kpis: dict[str, Any] | None, now: float
) -> dict[str, Any]:
    """The published numbers. Coarse on purpose: one up flag for all stacks,
    uptime in days and backup as ok or not, so restarts and failures of one
    service are not announced to the second."""
    project = "container_label_com_docker_compose_project"
    running = {r["metric"].get(project) for r in results["running"]}
    if not running:
        # cAdvisor or Prometheus is not reporting; publishing "all down" would be a lie.
        raise StatsError("no running containers reported")
    ha_started = scalar(results["ha_started"])
    backup_at = scalar(results["backup_at"])
    blocked = whole(scalar(results["blocked"]))
    if blocked is None:
        # A blank count would single out CrowdSec; let the last numbers age.
        raise StatsError("no CrowdSec decisions reported")
    if not isinstance(kpis, dict) or kpis.get("apiHealth") != "live":
        kpis = {}
    return {
        "at": int(now),
        "allUp": all(stack in running for stack in STACKS),
        "blocked": blocked,
        "tracking": whole(kpis.get("trackedAircraft")),
        "londonHour": whole(kpis.get("throughputLast60Min")),
        # Clock skew could make it negative, which the blog refuses.
        "haDays": None if ha_started is None else whole(max(0, (now - ha_started) // 86400)),
        "backupOk": backup_at is not None and now - backup_at < 26 * 3600,
    }


def gather(prometheus: str, flights: str) -> dict[str, Any]:
    results = {name: query(prometheus, promql) for name, promql in QUERIES.items()}
    try:
        kpis = get_json(flights).get("kpis")
    except (OSError, ValueError, AttributeError) as error:
        print(f"flight tracker: {error}", file=sys.stderr)
        kpis = None
    return build_body(results, kpis, time.time())


def push(url: str, token: str, body: dict[str, Any]) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_S):
        pass


def main() -> None:
    prometheus = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
    flights = os.environ["FLIGHTS_URL"]
    url = os.environ["PUBLIC_STATS_URL"]
    token = os.environ["PUBLIC_STATS_TOKEN"]
    while True:
        try:
            push(url, token, gather(prometheus, flights))
            # The healthcheck: fresh while pushes succeed.
            HEARTBEAT.touch()
            wait = EVERY_S
        except Exception as error:
            # Anything odd in a response skips one push, not the process.
            # HTTPError's message says why the Worker refused.
            print(f"push failed: {error!r}", file=sys.stderr)
            wait = RETRY_S
        time.sleep(wait)


if __name__ == "__main__":
    main()
