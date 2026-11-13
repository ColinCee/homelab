#!/usr/bin/env python3
"""Record how long this repo's CI and deploy runs take.

Each finished run attempt becomes one trace in the trace store: the run, its jobs and steps, and,
for Colin's own runs, the slowest tests and the job log's phases to the millisecond (each step, each
mise task and command). GitHub's API times steps only to the second. The latest
successful run's job seconds, by kind (main or pr), go to ci-times.prom for Prometheus and the
ci-slower alert.

Neither CI host can reach the observability stack, so this asks GitHub, with the CI supervisor's
token. Installed by ci-runner/install_host.py and run every 5 minutes by homelab-ci-times.timer.
See docs/architecture.md, "CI runner".
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import telemetry

REPO = "ColinCee/homelab"
API = "https://api.github.com"
OWNER_ID = 11246116
TOKEN_FILE = Path("/etc/homelab-ci/github-token")
OUT = Path("/var/lib/homelab-metrics/ci-times.prom")
STATE = Path("/var/lib/homelab-ci/ci-times.json")
# The run attempts already sent, newest kept: older ones have left GitHub's first page.
SENT_KEEP = 500
# The tests' annotation (ci.yaml, "Report the slowest tests"). A run's own code writes it, so it is
# read only for Colin's own runs, and capped.
TESTS_TITLE = "slowest tests"
TESTS_MAX = 50
NAME_MAX = 200
# Anyone's run is traced, and a fork's matrix could make thousands of spans.
SPANS_MAX = 500
# A job log's line: its time, then the text (mise's colours stripped, as escapes or as ^[ text).
LOG_LINE = re.compile(r"^\ufeff?(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?)Z (.*)$")
COLOUR = re.compile(r"(?:\x1b|\^\[)\[[0-9;]*m")
# mise's task lines: "[task] $ command" as a command starts, "[task] Finished in 1.2s" at its end.
TASK_LINE = re.compile(r"^\[([\w:.-]+)\] (?:\$ (.*)|Finished in ([\d.]+)(ms|s)$)")
LOG_MAX = 2_000_000
# A job unseen this long (renamed, removed) drops out of the metrics.
ROW_DAYS = 30

log = logging.getLogger("ci-times")
Get = Callable[[str], Any]
# A job's log text by job id, "" when it cannot be had.
Logs = Callable[[int], str]
# A JSON object from GitHub, or one sent to Alloy as OTLP.
Json = dict[str, Any]


def seconds(value: object) -> float:
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
    return kind(run) == "main" or bool(
        actor.get("id") == OWNER_ID and triggering.get("id") == OWNER_ID
    )


def kind(run: Mapping[str, Any]) -> str:
    """The metrics' label. The branch itself is only in the trace: a series per branch would
    grow forever."""
    if run.get("event") == "push" and run.get("head_branch") == "main":
        return "main"
    return "pr" if run.get("event") == "pull_request" else str(run.get("event"))


def slowest_tests(get: Get, job_id: int) -> list[dict[str, Any]]:
    """The job's annotation of its slowest tests, [[test, start, seconds], ...] as
    conftest.py writes it; [] when absent or malformed."""
    for note in get(f"/repos/{REPO}/check-runs/{job_id}/annotations?per_page=50"):
        if note.get("title") != TESTS_TITLE:
            continue
        try:
            return [
                {"test": str(t[0])[:NAME_MAX], "start": float(t[1]), "seconds": float(t[2])}
                for t in json.loads(note.get("message", ""))[:TESTS_MAX]
            ]
        except ValueError, TypeError, IndexError:
            return []
    return []


def span_id(*parts: object) -> str:
    return hashlib.sha256("/".join(map(str, parts)).encode()).hexdigest()[:16]


def log_time(text: str) -> float:
    whole, _, fraction = text.partition(".")
    return datetime.fromisoformat(f"{whole}+00:00").timestamp() + float(f"0.{fraction or 0}")


def log_spans(trace: str, job: str, log: str) -> list[Json]:
    """The job's phases from its log, to the millisecond: setup, each step (by its first line), the
    post-job steps, and inside them each mise task and its commands."""
    lines: list[tuple[float, str]] = []
    for raw in log.split("\n"):
        if m := LOG_LINE.match(raw.rstrip("\r")):
            try:
                lines.append((log_time(m.group(1)), COLOUR.sub("", m.group(2))))
            except ValueError:
                continue
    if not lines:
        return []
    end = lines[-1][0]
    starts = [(lines[0][0], "set up job")]
    for at, text in lines:
        if text.startswith("##[group]Run "):
            starts.append((at, "step: " + text.removeprefix("##[group]Run ")))
        elif text.startswith("A job started hook"):
            starts.append((at, "job started hook"))
        elif text.startswith("Post job cleanup"):
            starts.append((at, "post job"))
    phases = [
        (span_id(job, "log", n), name, at, starts[n + 1][0] if n + 1 < len(starts) else end)
        for n, (at, name) in enumerate(starts)
    ]
    spans = [
        telemetry.otlp_span(trace, sid, job, name, at, until, source="log")
        for sid, name, at, until in phases
    ]

    def phase(at: float) -> str:
        return next((sid for sid, _, start, until in phases if start <= at <= until), job)

    tasks: dict[str, tuple[str, float]] = {}  # a task's span id and start
    commands: dict[str, tuple[str, float, str]] = {}  # a task's running command: id, start, text
    for n, (at, text) in enumerate(lines):
        if not (m := TASK_LINE.match(text)):
            continue
        task, command = m.group(1), m.group(2)
        if task not in tasks:
            tasks[task] = (span_id(job, "task", task, n), at)
        tid, start = tasks[task]
        if task in commands and (command is not None or m.group(3)):
            cid, began, said = commands.pop(task)
            spans.append(telemetry.otlp_span(trace, cid, tid, said, began, at, source="log"))
        if command is not None:
            commands[task] = (span_id(tid, "command", n), at, command)
        else:
            spans.append(
                telemetry.otlp_span(
                    trace, tid, phase(start), f"task: {task}", start, at, source="log"
                )
            )
            del tasks[task]
    for task, (cid, began, said) in commands.items():
        spans.append(
            telemetry.otlp_span(
                trace, cid, tasks[task][0], said, began, end, source="log", ended="no"
            )
        )
    for task, (tid, start) in tasks.items():
        spans.append(
            telemetry.otlp_span(trace, tid, phase(start), f"task: {task}", start, end, ended="no")
        )
    return spans


def window(item: Mapping[str, Any]) -> tuple[float, float] | None:
    if not (item.get("started_at") and item.get("completed_at")):
        return None
    return seconds(item["started_at"]), seconds(item["completed_at"])


def trace(
    run: Mapping[str, Any],
    attempt: int,
    jobs: list[Mapping[str, Any]],
    tests: Mapping[int, list[Json]],
    logs: Mapping[int, str] | None = None,
) -> Json:
    """One OTLP trace for a run attempt. Its IDs come from the run and attempt, so sending it
    twice stores the same spans. Each level of span is its own service, as Grafana's timeline
    colours a span by its service."""
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
    spans = [("ci", telemetry.otlp_span(tid, root, "", str(run.get("name")), start, end, **about))]
    for job, (job_start, job_end) in timed:
        jid = span_id(run["id"], attempt, job["id"])
        runner = runner_kind(job.get("labels", []))
        conclusion = job.get("conclusion")
        name = str(job.get("name"))
        job_span = telemetry.otlp_span(
            tid, jid, root, name, job_start, job_end, runner=runner, conclusion=conclusion
        )
        spans.append(("ci job", job_span))
        for step in job.get("steps", []):
            if not (w := window(step)):
                continue
            sid = span_id(jid, step.get("number"))
            name = str(step.get("name"))
            step_span = telemetry.otlp_span(
                tid, sid, jid, name, *w, conclusion=step.get("conclusion")
            )
            spans.append(("ci step", step_span))
            if not name.startswith("Run checks"):
                continue
            for test in tests.get(int(job["id"]), []):
                # The test host's clock, kept inside its step's.
                t_start = min(max(test["start"], w[0]), w[1])
                t_end = min(t_start + test["seconds"], w[1])
                test_span = telemetry.otlp_span(
                    tid, span_id(sid, test["test"]), sid, test["test"], t_start, t_end
                )
                spans.append(("ci test", test_span))
    for job, _ in timed:
        jid = span_id(run["id"], attempt, job["id"])
        log = (logs or {}).get(int(job["id"]), "")
        spans += [("ci task", log_span) for log_span in log_spans(tid, jid, log)]
    by_service: dict[str, list[Json]] = {}
    for service, one in spans[:SPANS_MAX]:
        by_service.setdefault(service, []).append(one)
    return telemetry.otlp_traces(by_service, repository=REPO)


def metrics(latest: Mapping[str, Mapping[str, Any]], now: float) -> str:
    """Each recent job's latest seconds. Jobs only: step names change with each pinned action's
    version, so their series would pile up; steps are in the traces. `ci_job`, as the scrape sets
    `job` itself."""
    lines: list[str] = []
    for row in latest.values():
        if row["at"] < now - ROW_DAYS * 86400:
            continue
        keys = (
            f'kind="{telemetry.label(row["kind"])}",workflow="{telemetry.label(row["workflow"])}",'
        )
        keys += f'ci_job="{telemetry.label(row["job"])}",runner="{telemetry.label(row["runner"])}"'
        lines.append(f"homelab_ci_job_seconds{{{keys}}} {row['seconds']:.0f}")
    return "\n".join(lines) + "\n"


def latest_rows(run: Mapping[str, Any], jobs: list[Mapping[str, Any]]) -> dict[str, Json]:
    rows: dict[str, Json] = {}
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
    send: Callable[[Json], None],
    state: Mapping[str, Any],
    keep: Callable[[Json], None] = lambda state: None,
    logs: Logs = lambda job_id: "",
) -> Json:
    """Send each new finished run attempt's trace, oldest first, and keep the latest rows of
    Colin's own runs: job names come from the run's workflow file, which any pull request may
    change. `done` is False when GitHub or Alloy could not be reached, and the rest wait.
    `keep` saves the state after each run, so a pass cut short loses nothing."""
    sent: list[str] = list(state.get("sent", []))
    latest: dict[str, Json] = dict(state.get("latest", {}))
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
            texts = {int(j["id"]): logs(int(j["id"])) for j in jobs if own}
            send(trace(run, attempt, jobs, tests, texts))
        except urllib.error.HTTPError as e:
            if not 400 <= e.code < 500:
                log.warning("stopped at a run", extra={"run": key, "status": e.code})
                return {"sent": sent[-SENT_KEEP:], "latest": latest, "done": False}
            # Refused for what it is (too big, say): skipped, so it cannot hold up newer runs.
            log.warning("skipped a run", extra={"run": key, "status": e.code})
            sent.append(key)
            keep({"sent": sent[-SENT_KEEP:], "latest": latest, "done": False})
            continue
        except OSError as e:
            log.warning("stopped at a run", extra={"run": key, "error": f"{type(e).__name__}: {e}"})
            return {"sent": sent[-SENT_KEEP:], "latest": latest, "done": False}
        if own and run.get("conclusion") == "success":
            # A job this run did not have (deploy, skipped) keeps its row from the last that did.
            latest |= latest_rows(run, jobs)
        sent.append(key)
        keep({"sent": sent[-SENT_KEEP:], "latest": latest, "done": False})
    return {"sent": sent[-SENT_KEEP:], "latest": latest, "done": True}


def github(token: str) -> Get:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    def get(path: str) -> Any:  # noqa: ANN401 - GitHub's JSON, read as each endpoint documents it
        return telemetry.http_json(f"{API}{path}", headers=headers, tries=2)

    return get


def github_logs(token: str) -> Logs:
    """A job's log: GitHub answers with a signed link to it, followed without the token (telemetry
    sends no header on through a redirect). "" when the log is gone (404, 410) at either step: a
    skipped job gets a link to a log never written. Any other failure, after one retry, stops the
    pass as GitHub's other failures do, so the run is traced next pass with its log."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    def logs(job_id: int) -> str:
        url = f"{API}/repos/{REPO}/actions/jobs/{job_id}/logs"
        try:
            body = telemetry.http(url, headers=headers, tries=2, limit=LOG_MAX)
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return ""
            raise OSError(f"log of job {job_id}: {e}") from e
        except OSError as e:
            raise OSError(f"log of job {job_id}: {e}") from e
        return body.decode("utf-8", "replace")

    return logs


def main() -> int:
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    def keep(state: Json) -> None:
        telemetry.replace_file(STATE, json.dumps(state), 0o600)

    token = telemetry.secret(TOKEN_FILE)
    state = poll(github(token), telemetry.send_traces, state, keep, github_logs(token))
    keep(state)
    if not state["done"]:
        return 1
    # Rewritten only after a whole pass, so its age is ci-times-stale's measure.
    telemetry.replace_file(OUT, metrics(state["latest"], time.time()))
    return 0


if __name__ == "__main__":
    telemetry.start()
    raise SystemExit(main())
