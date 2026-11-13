"""Replays the alert rules (rules.yml and slos.rules.yml) over history and counts the Discord posts
each would have made, for the noise budget in docs/architecture.md ("Monitoring and alerts"),
which has the command.

    replay.py [days] [rule ...]

Reads Prometheus through the read-only gateway (GATEWAY_URL, default the Beelink's; GATEWAY_LOGIN,
user:password). Runs each rule's query a minute apart and follows Prometheus and the incidents
relay: a post once the query has returned for `for` and a "Cleared" once it has stayed empty for
`keep_firing_for`; a repeat posts nothing. Inhibits are not replayed."""

import os
import sys
import time
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Any

# The shared module, from this checkout (stacks/telemetry), and the rule files' loader.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "telemetry"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import telemetry

from stacks.observability.prometheus import slos

STEP = 60


def query_range(promql: str, start: int, end: int) -> list[dict[str, Any]]:
    url = os.environ.get("GATEWAY_URL", "http://beelink.tail4f465e.ts.net:5081")
    query = urllib.parse.urlencode({"query": promql, "start": start, "end": end, "step": STEP})
    answer = telemetry.http_json(
        f"{url}/prometheus/api/v1/query_range?{query}",
        login=os.environ["GATEWAY_LOGIN"],
        timeout=120,
        tries=2,
    )
    return answer["data"]["result"]


def history(promql: str, start: int, end: int) -> dict[str, set[int]]:
    """The minutes each series of the query returned, a day per request."""
    series: dict[str, set[int]] = defaultdict(set)
    while start < end:
        stop = min(start + 86400, end)
        for result in query_range(promql, start, stop):
            key = ",".join(f"{k}={v}" for k, v in sorted(result["metric"].items()))
            series[key] |= {int(float(t)) for t, _ in result["values"]}
        start = stop + STEP
    return series


def posts(returned: set[int], rule: dict[str, Any], start: int, end: int) -> list[tuple[int, str]]:
    wait, hold = slos.seconds(rule["for"]), slos.seconds(rule.get("keep_firing_for", "0m"))
    out: list[tuple[int, str]] = []
    since: int | None = None
    last_true = 0
    firing = False
    for t in range(start - start % STEP, end, STEP):
        if t in returned:
            since = t if since is None else since
            last_true = t
            if not firing and t - since >= wait:
                firing = True
                out.append((t, "firing"))
        else:
            since = None
            if firing and t - last_true >= hold:
                firing = False
                out.append((t, "cleared"))
    return out


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    only = set(sys.argv[2:])
    rules = [r for g in slos.rule_groups() for r in g["rules"]]
    end = int(time.time()) // STEP * STEP
    start = end - days * 86400
    total = 0
    for rule in rules:
        # The watchdog pings Healthchecks.io, never posts.
        if (only and rule["alert"] not in only) or rule["labels"]["level"] == "watchdog":
            continue
        found = [
            (t, kind, key)
            for key, returned in history(rule["expr"], start, end).items()
            for t, kind in posts(returned, rule, start, end)
        ]
        total += len(found)
        print(f"{rule['alert']} ({rule['labels']['level']}): {len(found)} posts in {days} days")
        for t, kind, key in sorted(found)[:10]:
            print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(t))} {kind} {key}")
    print(f"All rules: {total} posts in {days} days")


if __name__ == "__main__":
    main()
