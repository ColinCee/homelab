"""Replays alerts.toml's rules over history and counts the Discord posts each would have made, for
the noise budget in docs/observability.md ("Alerts"), which has the command.

    replay.py alerts.toml [days] [rule ...]

Reads OpenObserve, and runs each rule's query as OpenObserve does (apply.py, `query`), so `for` and
the hold are replayed as they run. Follows Alertmanager's lifecycle
(../alertmanager/alertmanager.yml): a post when the query first fires, a repeat every 4 h (now)
or 24 h (week) while it fires, and a "Cleared" once it has stopped firing for resolve_timeout.
Inhibits are not replayed."""

import os
import re
import sys
import time
import tomllib
import urllib.parse
from collections import defaultdict
from collections.abc import Callable

# Beside apply.py; -I leaves the script's folder off the path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apply

STEP = 60
# Alertmanager's resolve_timeout.
RESOLVE = 5 * 60
REPEAT = {"now": 4 * 3600, "week": 24 * 3600}
OPERATORS = {">": float.__gt__, "<": float.__lt__, ">=": float.__ge__, "<=": float.__le__}

Range = Callable[[str, int, int], list[dict]]


def openobserve() -> Range:
    call = apply.client(
        os.environ.get("OPENOBSERVE_URL", "http://openobserve:5080"),
        os.environ.get("OPENOBSERVE_USER", "root@homelab.internal"),
        os.environ["OPENOBSERVE_PASSWORD"],
    )

    def query_range(promql: str, start: int, end: int) -> list[dict]:
        query = urllib.parse.urlencode({"query": promql, "start": start, "end": end, "step": STEP})
        return call("GET", f"/api/{apply.ORG}/prometheus/api/v1/query_range?{query}", None)["data"][
            "result"
        ]

    return query_range


def history(query_range: Range, promql: str, start: int, end: int) -> dict[str, dict[int, float]]:
    """Each series' values by minute, a day per request."""
    series: dict[str, dict[int, float]] = defaultdict(dict)
    while start < end:
        stop = min(start + 86400, end)
        for result in query_range(promql, start, stop):
            key = ",".join(f"{k}={v}" for k, v in sorted(result["metric"].items()))
            series[key] |= {int(float(t)): float(v) for t, v in result["values"]}
        start = stop + STEP
    return series


def posts(values: dict[int, float], rule: dict, start: int, end: int) -> list[tuple[int, str]]:
    _, operator, threshold = apply.query(rule)
    out: list[tuple[int, str]] = []
    clear = 0
    firing = False
    last_post = 0
    for t in range(start - start % STEP, end, STEP):
        v = values.get(t)
        if v is not None and OPERATORS[operator](v, float(threshold)):
            clear = 0
            if not firing:
                firing, last_post = True, t
                out.append((t, "firing"))
            elif t - last_post >= REPEAT[rule["level"]]:
                last_post = t
                out.append((t, "repeat"))
        elif firing:
            clear += STEP
            if clear >= RESOLVE:
                out.append((t, "cleared"))
                firing = False
    return out


def main() -> None:
    with open(sys.argv[1], "rb") as file:
        config = tomllib.load(file)
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    only = set(sys.argv[3:])
    query_range = openobserve()
    end = int(time.time()) // STEP * STEP
    start = end - days * 86400
    total = 0
    for uid, rule in config["rules"].items():
        if only and uid not in only:
            continue
        # A no-data rule's own selector, which may pick one file of a metric; else the metric.
        absent = re.search(r"absent_over_time\((.+)\[", rule.get("no_data", ""))
        metric = absent[1] if absent else apply.stream(rule["promql"])
        seen = [
            t
            for values in history(query_range, f"count({metric})", start, end).values()
            for t in values
        ]
        if not seen:
            print(f"{uid}: no history of {metric}")
            continue
        # From once the query's windows hold only time after the metric's first sample: before
        # that a no-data query, or the hold over one, sees a metric not yet made.
        first = min(seen)
        full = first + apply.seconds(rule["for"]) + apply.seconds(apply.HOLD)
        found = []
        for key, values in history(query_range, apply.query(rule)[0], first, end).items():
            found += [(t, kind, key) for t, kind in posts(values, rule, full, end)]
        total += len(found)
        print(f"{uid} ({rule['level']}): {len(found)} posts in {days} days", flush=True)
        for t, kind, key in sorted(found)[:10]:
            print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(t))} {kind} {key}")
    print(f"All rules: {total} posts in {days} days")


if __name__ == "__main__":
    main()
