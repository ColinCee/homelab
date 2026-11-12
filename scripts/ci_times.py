#!/usr/bin/env python3
"""Record how long this repo's CI and deploy runs take.

Each finished run attempt becomes one trace in the trace store: the run, its jobs and steps, and
the slowest tests of Colin's own runs. The latest successful run's job and step seconds, by kind
(main or pr), go to ci-times.prom for Prometheus and the ci-slower alert.

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

Get = Callable[[str], Any]


def seconds(value: Any) -> float:
    return datetime.fromisoformat(str(value)).timestamp()


def runner_kind(labels: Iterable[str]) -> str:
    asked = {label.lower() for label in labels}
    if "homelab-fc" in asked:
        return "vm"
    return "beelink" if "beelink" in asked else "github"


def trusted(run: Mapping[str, Any]) -> bool:
    """A run of Colin's from this repo, not a fork, that he also re-ran if it is a re-run."""
    actor, triggering = run.get("actor") or {}, run.get("triggering_actor") or {}
    head = (run.get("head_repository") or {}).get("full_name")
    return actor.get("id") == OWNER_ID and triggering.get("id") == OWNER_ID and head == REPO


def kind(run: Mapping[str, Any]) -> str:
    """The metrics' label. The branch itself is only in the trace: a series per branch would
    grow forever."""
    if run.get("event") == "push" and run.get("head_branch") == "main":
        return "main"
    return "pr" if run.get("event") == "pull_request" else str(run.get("event"))


def slowest_tests(get: Get, job_id: int) -> list[dict[str, Any]]:
    """The job's annotation of its slowest tests, [] when absent or malformed."""
    for note in get(f"/repos/{REPO}/check-runs/{job_id}/annotations?per_page=50"):
        if note.get("title") != TESTS_TITLE:
            continue
        try:
            return [
                {
                    "test": str(t["test"])[:NAME_MAX],
                    "start": float(t["start"]),
                    "seconds": float(t["seconds"]),
                }
                for t in json.loads(note.get("message", ""))[:TESTS_MAX]
            ]
        except (ValueError, TypeError, KeyError):
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
    return {"resourceSpans": [{"resource": resource, "scopeSpans": [{"spans": spans}]}]}


def label(text: str) -> str:
    return re.sub(r'["\\\n]', "_", text)


def metrics(latest: Mapping[str, Mapping[str, Any]]) -> str:
    """Each latest row's seconds. The file is rewritten each run, so its age is the no-data alarm,
    since main may go days without a push."""
    lines: list[str] = []
    for row in latest.values():
        keys = ",".join(
            f'{name}="{label(str(row[name]))}"' for name in ("kind", "workflow", "job", "runner")
        )
        if row["step"]:
            keys += f',step="{label(row["step"])}"'
            lines.append(f"homelab_ci_step_seconds{{{keys}}} {row['seconds']:.0f}")
        else:
            lines.append(f"homelab_ci_job_seconds{{{keys}}} {row['seconds']:.0f}")
    return "\n".join(lines) + "\n"


def latest_rows(run: Mapping[str, Any], jobs: list[Mapping[str, Any]]) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for job in jobs:
        if job.get("conclusion") != "success" or not (w := window(job)):
            continue
        base = {
            "kind": kind(run),
            "workflow": str(run.get("name")),
            "job": str(job.get("name")),
            "runner": runner_kind(job.get("labels", [])),
        }
        prefix = "/".join(base.values())
        rows[f"{prefix}/"] = base | {"step": "", "seconds": w[1] - w[0]}
        for step in job.get("steps", []):
            if s := window(step):
                name = str(step.get("name"))[:NAME_MAX]
                rows[f"{prefix}/{name}"] = base | {"step": name, "seconds": s[1] - s[0]}
    return rows


def poll(get: Get, send: Callable[[dict], None], state: Mapping[str, Any], now: float) -> dict:
    """Send each new finished run attempt's trace, oldest first, and keep the latest rows of
    Colin's own runs: step names come from the run's workflow file, which any pull request may
    change."""
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
        except OSError as e:
            # The rest wait for the next run; what is done so far is kept.
            print(f"stopped at run {key}: {type(e).__name__}: {e}")
            break
        if own and run.get("conclusion") == "success":
            # A job this run did not have (deploy, skipped) keeps its row from the last that did.
            latest |= latest_rows(run, jobs)
        sent.append(key)
    return {"sent": sent[-SENT_KEEP:], "latest": latest, "polled": now}


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
    state = poll(github(TOKEN_FILE.read_text().strip()), send_trace, state, time.time())
    replace(STATE, json.dumps(state), 0o600)
    replace(OUT, metrics(state["latest"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
