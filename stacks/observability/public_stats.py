"""Push a few read-only numbers about this server to the blog's home page.

Runs in this stack's public-stats container. Every five minutes
it asks Prometheus, through the stack's read-only gateway, and the already-public
flight-tracker API for a handful of numbers and PUTs them to the blog, which shows
them in its home-lab figure.
The body is built from named numbers only, never by forwarding a response, so
nothing else about the host can leak. Anyone can poll the page and keep a
history, so the numbers are coarse: one flag for all stacks, and no backup or
per-service uptime, which over time would show outage windows. The blog's
Worker refuses any other field, an `at` over 10 minutes old, and numbers not
newer than the ones it has, so a drifting clock shows as refused pushes here.
See docs/architecture.md, "Public stats".
"""

import json
import logging
import math
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

import telemetry
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# The stacks in this repository; tests/test_public_stats.py keeps it in step.
STACKS = (
    "backup",
    "deploy",
    "flight-tracker",
    "home-assistant",
    "internal-names",
    "observability",
    "tunnel",
)
EVERY_S = 300
# After a failure, sooner: the first push decides whether a deploy's
# readiness wait sees the container healthy.
RETRY_S = 15
TIMEOUT_S = 15
HEARTBEAT = Path("/tmp/ready")

QUERIES = {
    # Stacks with a container seen running in the last minute.
    "running": "count by (container_label_com_docker_compose_project) "
    '(time() - container_last_seen{container_label_com_docker_compose_project!=""} < 60)',
}


class StatsError(Exception):
    pass


log = logging.getLogger("public-stats")


def prometheus_login() -> str | None:
    """The gateway's `user:password`, from PROMETHEUS_USER and PROMETHEUS_PASSWORD_FILE."""
    password_file = os.environ.get("PROMETHEUS_PASSWORD_FILE")
    if not password_file:
        return None
    password = telemetry.secret(password_file)
    return f"{os.environ['PROMETHEUS_USER']}:{password}"


def query(prometheus: str, promql: str) -> list[dict[str, Any]]:
    url = f"{prometheus}/api/v1/query?" + urllib.parse.urlencode({"query": promql})
    body = telemetry.http_json(url, login=prometheus_login(), timeout=TIMEOUT_S)
    if body.get("status") != "success":
        raise StatsError(f"prometheus: {body.get('error', 'query failed')}")
    return body["data"]["result"]


def whole(value: object) -> int | None:
    """A count the blog accepts, or None for anything else (null, NaN, +Inf)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    # The blog's Worker takes 0 to 9,999,999; outside that the push is refused.
    return int(value) if math.isfinite(value) and 0 <= value < 10_000_000 else None


def build_body(
    results: dict[str, list[dict[str, Any]]], kpis: dict[str, Any] | None, now: float
) -> dict[str, Any]:
    """The published numbers. Coarse on purpose: one up flag for all stacks,
    so a failure of one service is not announced. Uptime and backup state
    were left out: polled over time they would show restart and failed-backup
    windows."""
    project = "container_label_com_docker_compose_project"
    running = {r["metric"].get(project) for r in results["running"]}
    if not running:
        # cAdvisor or Prometheus is not reporting; publishing "all down" would be a lie.
        raise StatsError("no running containers reported")
    if not isinstance(kpis, dict) or kpis.get("apiHealth") != "live":
        kpis = {}
    return {
        "at": int(now),
        "allUp": all(stack in running for stack in STACKS),
        "tracking": whole(kpis.get("trackedAircraft")),
        "londonHour": whole(kpis.get("throughputLast60Min")),
    }


def gather(prometheus: str, flights: str) -> dict[str, Any]:
    results = {name: query(prometheus, promql) for name, promql in QUERIES.items()}
    try:
        kpis = telemetry.http_json(flights, timeout=TIMEOUT_S).get("kpis")
    except (OSError, ValueError, AttributeError) as error:
        log.error("cannot read the flight tracker", extra={"error": str(error)})
        kpis = None
    return build_body(results, kpis, time.time())


def signed(key: Ed25519PrivateKey, body: dict[str, Any]) -> tuple[bytes, str]:
    """The body as sent, and its signature, which the blog's Worker checks
    against the public half of this key."""
    data = json.dumps(body).encode()
    return data, key.sign(data).hex()


def push(url: str, key: Ed25519PrivateKey, body: dict[str, Any]) -> None:
    data, signature = signed(key, body)
    headers = {"Signature": signature, "Content-Type": "application/json"}
    telemetry.http(url, method="PUT", data=data, headers=headers, timeout=TIMEOUT_S)


def main() -> None:
    prometheus = os.environ["PROMETHEUS_URL"]
    flights = os.environ["FLIGHTS_URL"]
    url = os.environ["PUBLIC_STATS_URL"]
    # The only copy of the private key is the PUBLIC_STATS_KEY GitHub secret.
    key_hex = telemetry.secret(os.environ["PUBLIC_STATS_KEY_FILE"])
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(key_hex))
    while True:
        try:
            push(url, key, gather(prometheus, flights))
            # The healthcheck: fresh while pushes succeed.
            HEARTBEAT.touch()
            wait = EVERY_S
        except Exception:
            # Anything odd in a response skips one push, not the process.
            # HTTPError's message says why the Worker refused.
            log.exception("push failed")
            wait = RETRY_S
        time.sleep(wait)


if __name__ == "__main__":
    telemetry.start()
    main()
