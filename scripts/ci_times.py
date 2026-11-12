#!/usr/bin/env python3
"""Record how long this repo's CI and deploy runs take.

Each finished run attempt becomes one trace in the trace store: the run, its jobs and steps, and
the slowest tests of Colin's own runs. The latest successful run's job seconds, by kind (main or
pr), go to ci-times.prom for Prometheus and the ci-slower alert.

Neither CI host can reach the observability stack, so this asks GitHub, with the CI supervisor's
token. Installed by ci-runner/install-host.sh and run every 5 minutes by homelab-ci-times.timer.
See docs/architecture.md, "CI runner".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = "ColinCee/homelab"
OWNER_ID = 11246116
TOKEN_FILE = Path("/etc/homelab-ci/github-token")
OUT = Path("/var/lib/homelab-metrics/ci-times.prom")
STATE = Path("/var/lib/homelab-ci/ci-times.json")
# Alloy's OTLP receiver at its fixed address on host-otlp (stacks/observability/compose.yaml).
TRACES = "http://10.201.0.10:4318/v1/traces"
# The run attempts already sent, newest kept: older ones have left GitHub's first page.
SENT_KEEP = 500
# The tests' annotation (ci.yaml, "Report the slowest tests"). A run's own code writes it, so it is
# read only for Colin's own runs, and capped.
TESTS_TITLE = "slowest tests"
TESTS_MAX = 50
NAME_MAX = 200
# Anyone's run is traced, and a fork's matrix could make thousands of spans.
SPANS_MAX = 500
# A job unseen this long (renamed, removed) drops out of the metrics.
ROW_DAYS = 30

Get = Callable[[str], Any]


def seconds(value: Any) -> float:
    return datetime.fromisoformat(str(value)).timestamp()


def runner_kind(labels: Iterable[str]) -> str:
    asked = {label.lower() for label in labels}
    if "homelab-fc" in asked:
        return "vm"
    return "beelink" if "beelink" in asked else "github"


def trusted(run: Mapping[str, Any]) -> bool:
    """From this repo, not a fork, and either a push to main (reviewed code, whoever merged it:
    Renovate's merges too) or a run Colin started and, if re-run, re-ran."""
    actor, triggering = run.get("actor") or {}, run.get("triggering_actor") or {}
    if (run.get("head_repository") or {}).get("full_name") != REPO:
        return False
    return kind(run) == "main" or (actor.get("id") == OWNER_ID and triggering.get("id") == OWNER_ID)


def kind(run: Mapping[str, Any]) -> str:
    """The metrics' label. The branch itself is only in the trace: a series per branch would
    grow forever."""
    if run.get("event") == "push" and run.get("head_branch") == "main":
        return "main"
    return "pr" if run.get("event") == "pull_request" else str(run.get("event"))


def slowest_tests(get: Get, job_id: int) -> list[dict[str, Any]]:
    """The job's annotation of its slowest tests, [[test, start, seconds], ...] as
    tests/conftest.py writes it; [] when absent or malformed."""
    for note in get(f"/repos/{REPO}/check-runs/{job_id}/annotations?per_page=50"):
        if note.get("title") != TESTS_TITLE:
            continue
        try:
            return [
                {"test": str(t[0])[:NAME_MAX], "start": float(t[1]), "seconds": float(t[2])}
                for t in json.loads(note.get("message", ""))[:TESTS_MAX]
            ]
        except (ValueError, TypeError, IndexError):
            return []
    return []


def span_id(*parts: object) -> str:
    return hashlib.sha256("/".join(map(str, parts)).encode()).hexdigest()[:16]


def span(
    trace: str, sid: str, parent: str, name: str, start: float, end: float, **attrs: object
) -> dict:
    return {
        "traceId": trace,
        "spanId": sid,
        "parentSpanId": parent,
        "name": name[:NAME_MAX],
        "kind": 1,
        "startTimeUnixNano": str(int(start * 1e9)),
        "endTimeUnixNano": str(int(max(start, end) * 1e9)),
        "attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in attrs.items()],
    }


def window(item: Mapping[str, Any]) -> tuple[float, float] | None:
    if not (item.get("started_at") and item.get("completed_at")):
        return None
    return seconds(item["started_at"]), seconds(item["completed_at"])


def trace(
    run: Mapping[str, Any], attempt: int, jobs: list[Mapping[str, Any]], tests: Mapping[int, list]
) -> dict:
    """One OTLP trace for a run attempt. Its IDs come from the run and attempt, so sending it
    twice stores the same spans."""
    tid = hashlib.sha256(f"{REPO}/{run['id']}/{attempt}".encode()).hexdigest()[:32]
    root = span_id(run["id"], attempt)
    timed = [(job, w) for job in jobs if (w := window(job))]
    start = min((w[0] for _, w in timed), default=seconds(run["run_started_at"]))
    end = max((w[1] for _, w in timed), default=start)
    about = {
        "event": run.get("event"),
        "branch": run.get("head_branch"),
        "conclusion": run.get("conclusion"),
        "attempt": attempt,
        "url": run.get("html_url"),
    }
    spans = [span(tid, root, "", str(run.get("name")), start, end, **about)]
    for job, (job_start, job_end) in timed:
        jid = span_id(run["id"], attempt, job["id"])
        runner = runner_kind(job.get("labels", []))
        conclusion = job.get("conclusion")
        name = str(job.get("name"))
        spans.append(
            span(tid, jid, root, name, job_start, job_end, runner=runner, conclusion=conclusion)
        )
        for step in job.get("steps", []):
            if not (w := window(step)):
                continue
            sid = span_id(jid, step.get("number"))
            name = str(step.get("name"))
            spans.append(span(tid, sid, jid, name, *w, conclusion=step.get("conclusion")))
            if not name.startswith("Run checks"):
                continue
            for test in tests.get(int(job["id"]), []):
                # The test host's clock, kept inside its step's.
                t_start = min(max(test["start"], w[0]), w[1])
                t_end = min(t_start + test["seconds"], w[1])
                spans.append(
                    span(tid, span_id(sid, test["test"]), sid, test["test"], t_start, t_end)
                )
    resource = {
        "attributes": [
            {"key": "service.name", "value": {"stringValue": "ci"}},
            {"key": "repository", "value": {"stringValue": REPO}},
        ]
    }
    scope = {"spans": spans[:SPANS_MAX]}
    return {"resourceSpans": [{"resource": resource, "scopeSpans": [scope]}]}


def label(text: str) -> str:
    return re.sub(r'["\\\n]', "_", text)


def metrics(latest: Mapping[str, Mapping[str, Any]], now: float) -> str:
    """Each recent job's latest seconds. Jobs only: step names change with each pinned action's
    version, so their series would pile up; steps are in the traces. `ci_job`, as the scrape sets
    `job` itself."""
    lines: list[str] = []
    for row in latest.values():
        if row["at"] < now - ROW_DAYS * 86400:
            continue
        keys = f'kind="{label(row["kind"])}",workflow="{label(row["workflow"])}",'
        keys += f'ci_job="{label(row["job"])}",runner="{label(row["runner"])}"'
        lines.append(f"homelab_ci_job_seconds{{{keys}}} {row['seconds']:.0f}")
    return "\n".join(lines) + "\n"


def latest_rows(run: Mapping[str, Any], jobs: list[Mapping[str, Any]]) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for job in jobs:
        if job.get("conclusion") != "success" or not (w := window(job)):
            continue
        row = {
            "kind": kind(run),
            "workflow": str(run.get("name")),
            "job": str(job.get("name"))[:NAME_MAX],
            "runner": runner_kind(job.get("labels", [])),
        }
        rows["/".join(row.values())] = row | {"seconds": w[1] - w[0], "at": w[1]}
    return rows


def poll(
    get: Get,
    send: Callable[[dict], None],
    state: Mapping[str, Any],
    keep: Callable[[dict], None] = lambda state: None,
) -> dict:
    """Send each new finished run attempt's trace, oldest first, and keep the latest rows of
    Colin's own runs: job names come from the run's workflow file, which any pull request may
    change. `done` is False when GitHub or Alloy could not be reached, and the rest wait.
    `keep` saves the state after each run, so a pass cut short loses nothing."""
    sent: list[str] = list(state.get("sent", []))
    latest: dict[str, dict] = dict(state.get("latest", {}))
    path = f"/repos/{REPO}/actions/runs?status=completed&per_page=30"
    runs = get(path).get("workflow_runs", [])
    for run in sorted(runs, key=lambda r: (r["id"], r.get("run_attempt", 1))):
        attempt = int(run.get("run_attempt", 1))
        key = f"{run['id']}/{attempt}"
        if key in sent:
            continue
        path = f"/repos/{REPO}/actions/runs/{run['id']}/attempts/{attempt}/jobs?per_page=100"
        own = trusted(run)
        try:
            jobs = get(path).get("jobs", [])
            tests = {int(j["id"]): slowest_tests(get, int(j["id"])) for j in jobs if own}
            send(trace(run, attempt, jobs, tests))
        except urllib.error.HTTPError as e:
            if not 400 <= e.code < 500:
                print(f"stopped at run {key}: HTTP {e.code}")
                return {"sent": sent[-SENT_KEEP:], "latest": latest, "done": False}
            # Refused for what it is (too big, say): skipped, so it cannot hold up newer runs.
            print(f"skipped run {key}: HTTP {e.code}")
            sent.append(key)
            keep({"sent": sent[-SENT_KEEP:], "latest": latest, "done": False})
            continue
        except OSError as e:
            print(f"stopped at run {key}: {type(e).__name__}: {e}")
            return {"sent": sent[-SENT_KEEP:], "latest": latest, "done": False}
        if own and run.get("conclusion") == "success":
            # A job this run did not have (deploy, skipped) keeps its row from the last that did.
            latest |= latest_rows(run, jobs)
        sent.append(key)
        keep({"sent": sent[-SENT_KEEP:], "latest": latest, "done": False})
    return {"sent": sent[-SENT_KEEP:], "latest": latest, "done": True}


def github(token: str) -> Get:
    def get(path: str) -> Any:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        request = urllib.request.Request(f"https://api.github.com{path}", headers=headers)
        for tries_left in (1, 0):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)
            except OSError:
                if not tries_left:
                    raise
                time.sleep(2)

    return get


def send_trace(body: dict) -> None:
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    request = urllib.request.Request(TRACES, data, headers, method="POST")
    with urllib.request.urlopen(request, timeout=30):
        pass


def replace(path: Path, text: str, mode: int = 0o644) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as f:
        f.write(text)
    os.chmod(f.name, mode)
    os.replace(f.name, path)


def main() -> int:
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    def keep(state: dict) -> None:
        replace(STATE, json.dumps(state), 0o600)

    state = poll(github(TOKEN_FILE.read_text().strip()), send_trace, state, keep)
    keep(state)
    if not state["done"]:
        return 1
    # Rewritten only after a whole pass, so its age is ci-times-stale's measure.
    replace(OUT, metrics(state["latest"], time.time()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
