"""Replays the alert rules (rules.yml and slos.rules.yml) over history and counts the Discord posts
each would have made, for the noise budget in docs/architecture.md ("Monitoring and alerts"),
which has the command.

    replay.py [days] [rule ...]

Reads Prometheus, and the log store for FROM_LOGS, through the read-only gateway (GATEWAY_URL,
default the Beelink's; GATEWAY_LOGIN, user:password). Runs each rule's query a minute apart and
follows Prometheus and the incidents relay: a post once the query has returned for `for` and a
"Cleared" once it has stayed empty for `keep_firing_for`; a repeat posts nothing.
Inhibits are not replayed."""

import json
import os
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# The shared module, from this checkout (stacks/telemetry), and the rule files' loader.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "telemetry"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import telemetry

from stacks.observability.prometheus import slos

STEP = 60
# A rule over a count Alloy makes of log lines has no history from before the count existed; the
# log store keeps the lines, so it replays from them: the LogsQL query, the label its posts are by,
# and how long one line keeps the rule's query returning (its `offset`).
FROM_LOGS = {"errors-logged": ("level:in(error,fatal)", "service_name", 300)}


def gateway() -> str:
    return os.environ.get("GATEWAY_URL", "http://beelink.tail4f465e.ts.net:5081")


def log_history(query: str, by: str, window: int, start: int, end: int) -> dict[str, set[int]]:
    """The minutes the rule would have returned for each value of BY, a day per request."""
    series: dict[str, set[int]] = defaultdict(set)
    while start < end:
        stop = min(start + 86400, end)
        span = f"_time:[{iso(start)}, {iso(stop)})"
        logsql = f"{span} {query} | stats by (_time:1m, {by}) count() lines"
        raw = telemetry.http(
            f"{gateway()}/logs/select/logsql/query?" + urllib.parse.urlencode({"query": logsql}),
            login=os.environ["GATEWAY_LOGIN"],
            timeout=120,
            tries=2,
        )
        for row in map(json.loads, raw.decode().splitlines()):
            minute = int(datetime.fromisoformat(row["_time"].replace("Z", "+00:00")).timestamp())
            # Its lines came within the minute, so the rule returns from the next one on.
            returned = range(minute + STEP, minute + window + 1, STEP)
            series[f"{by}={row.get(by, '')}"] |= set(returned)
        start = stop
    return series


def iso(t: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def query_range(promql: str, start: int, end: int) -> list[dict[str, Any]]:
    url = gateway()
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
        logs = FROM_LOGS.get(rule["alert"])
        returned_by = log_history(*logs, start, end) if logs else history(rule["expr"], start, end)
        found = [
            (t, kind, key)
            for key, returned in returned_by.items()
            for t, kind in posts(returned, rule, start, end)
        ]
        total += len(found)
        print(f"{rule['alert']} ({rule['labels']['level']}): {len(found)} posts in {days} days")
        for t, kind, key in sorted(found)[:10]:
            print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(t))} {kind} {key}")
    print(f"All rules: {total} posts in {days} days")


if __name__ == "__main__":
    main()
