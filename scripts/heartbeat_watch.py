"""Posts to Discord when the Beelink's heartbeat (.github/workflows/heartbeat.yaml) has not passed
for 2 hours, again every 4 hours while it stays so, and "Cleared" once it passes again. Runs on
GitHub's machines (heartbeat-watch.yaml), so it works while the Beelink is down.

Each run decides from the heartbeat's successful runs and the time of this workflow's previous
run, so it keeps no state. Reads GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_RUN_ID and
DISCORD_WEBHOOK; with DRY_RUN=true it prints the post instead."""

import json
import math
import os
import sys
import urllib.request
from datetime import UTC, datetime

STALE = 2 * 3600
REMIND = 4 * 3600
COLIN = "124131392207454208"


def last_ok(successes: list[float], t: float) -> float | None:
    return max((s for s in successes if s <= t), default=None)


def overdue(successes: list[float], t: float) -> float:
    """Seconds past the 2 hours the heartbeat may go without passing; negative while it is fine."""
    ok = last_ok(successes, t)
    return math.inf if ok is None else t - ok - STALE


def decide(successes: list[float], now: float, previous: float | None) -> str | None:
    """`firing`, `cleared` or nothing, from the heartbeat's success times and the previous watch."""
    late = overdue(successes, now)
    was = overdue(successes, previous) if previous is not None else -1.0
    if late <= 0:
        return "cleared" if was > 0 else None
    if was <= 0:
        return "firing"
    # A reminder when this run is the first past the next 4-hour mark.
    if math.isinf(late):
        assert previous is not None
        return "firing" if now // REMIND > previous // REMIND else None
    return "firing" if late // REMIND > was // REMIND else None


def api(path: str) -> dict:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{path}",
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def seconds(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def message(kind: str, successes: list[float], now: float, runs: str) -> str:
    ok = last_ok(successes, now)
    when = "never" if ok is None else datetime.fromtimestamp(ok, UTC).strftime("%d %b %H:%M UTC")
    if kind == "cleared":
        return f"**Cleared: Alert monitoring stopped**\nThe heartbeat passed at {when}."
    return (
        f"<@{COLIN}> **Alert monitoring stopped**\n"
        f"The Beelink's heartbeat has not passed since {when}.\n"
        "**Why it matters:** while it fails, an alert may never reach you: OpenObserve, "
        "Alertmanager or the Beelink itself may be down.\n"
        f"**What to do:** open the latest run in {runs} to see which check failed; "
        "if none ran, the Beelink or its runner is down."
    )


def post(text: str) -> None:
    body = json.dumps({"content": text, "allowed_mentions": {"users": [COLIN]}}).encode()
    request = urllib.request.Request(
        os.environ["DISCORD_WEBHOOK"],
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "homelab-heartbeat-watch"},
    )
    with urllib.request.urlopen(request, timeout=30):
        pass


def main() -> None:
    now = datetime.now(UTC).timestamp()
    runs = api("/actions/workflows/heartbeat.yaml/runs?status=success&per_page=100")
    successes = [seconds(r["updated_at"]) for r in runs["workflow_runs"]]
    watches = api("/actions/workflows/heartbeat-watch.yaml/runs?per_page=10")["workflow_runs"]
    me = os.environ["GITHUB_RUN_ID"]
    previous = next(
        (
            seconds(r["created_at"])
            for r in watches
            if str(r["id"]) != me and r["status"] == "completed"
        ),
        None,
    )
    kind = decide(successes, now, previous)
    print(f"Heartbeat last passed {overdue(successes, now) + STALE:.0f} s ago; post: {kind}")
    if kind is None:
        return
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    text = message(
        kind,
        successes,
        now,
        f"{server}/{os.environ['GITHUB_REPOSITORY']}/actions/workflows/heartbeat.yaml",
    )
    if os.environ.get("DRY_RUN") == "true":
        print(text)
        return
    # A failed post fails the run, so GitHub tells Colin by email instead.
    post(text)


if __name__ == "__main__":
    sys.exit(main())
