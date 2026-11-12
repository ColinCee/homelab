"""CI run times (scripts/ci_times.py): one trace per run attempt, the latest of Colin's own runs as
metrics, and others' runs' own words kept out."""

import email.message
import http.server
import json
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from scripts import ci_times

COLIN = {"id": ci_times.OWNER_ID, "login": "ColinCee"}
OTHER = {"id": 1, "login": "someone"}


def run(
    n: int,
    *,
    actor: dict[str, Any] = COLIN,
    event: str = "push",
    branch: str = "main",
    head: str = ci_times.REPO,
    attempt: int = 1,
) -> dict[str, Any]:
    return {
        "id": n,
        "run_attempt": attempt,
        "name": "CI",
        "event": event,
        "head_branch": branch,
        "conclusion": "success",
        "actor": actor,
        "triggering_actor": actor,
        "head_repository": {"full_name": head},
        "run_started_at": "2026-11-12T00:00:00Z",
        "html_url": f"https://github.com/{ci_times.REPO}/actions/runs/{n}",
    }


def job(n: int, labels: Iterable[str] = ("self-hosted", "homelab-fc")) -> dict[str, Any]:
    return {
        "id": n,
        "name": "check",
        "conclusion": "success",
        "labels": list(labels),
        "started_at": "2026-11-12T00:00:10Z",
        "completed_at": "2026-11-12T00:00:40Z",
        "steps": [
            {
                "number": 1,
                "name": "Run checks (lint + typecheck + test + validate)",
                "conclusion": "success",
                "started_at": "2026-11-12T00:00:20Z",
                "completed_at": "2026-11-12T00:00:35Z",
            }
        ],
    }


def github(
    runs: list[dict[str, Any]], tests: list[list[Any]] | None = None
) -> tuple[ci_times.Get, list[str]]:
    asked: list[str] = []

    def get(path: str) -> dict[str, Any] | list[dict[str, Any]]:
        asked.append(path)
        if "/annotations" in path:
            message = json.dumps(tests or [])
            return [{"title": ci_times.TESTS_TITLE, "message": message}]
        if path.endswith("/jobs?per_page=100"):
            return {"jobs": [job(int(path.split("/runs/")[1].split("/")[0]) * 10)]}
        return {"workflow_runs": runs}

    return get, asked


def names(body: dict[str, Any]) -> list[str]:
    return [s["name"] for s in body["resourceSpans"][0]["scopeSpans"][0]["spans"]]


def test_each_run_attempt_is_sent_once_with_its_jobs_steps_and_tests() -> None:
    tests = [["tests/test_x.py::test_slow", 1794441625.0, 3.0]]
    get, _ = github([run(1), run(1, attempt=2)], tests)
    sent: list[dict[str, Any]] = []
    state = ci_times.poll(get, sent.append, {})
    assert len(sent) == 2
    assert names(sent[0]) == [
        "CI",
        "check",
        "Run checks (lint + typecheck + test + validate)",
        "tests/test_x.py::test_slow",
    ]
    # Different attempts, different traces; the same attempt again is not sent.
    assert (
        sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"]
        != (sent[1]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"])
    )
    again: list[dict[str, Any]] = []
    ci_times.poll(get, again.append, state)
    assert again == []


def test_a_test_span_stays_inside_its_step_whatever_its_clock_says() -> None:
    late = [["t", 9e9, 5.0]]
    get, _ = github([run(1)], late)
    sent: list[dict[str, Any]] = []
    ci_times.poll(get, sent.append, {})
    spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    step, test = spans[2], spans[3]
    assert step["startTimeUnixNano"] <= test["startTimeUnixNano"] <= step["endTimeUnixNano"]
    assert test["endTimeUnixNano"] <= step["endTimeUnixNano"]


def test_others_runs_are_traced_without_their_tests_or_metrics() -> None:
    fork = run(1, event="pull_request", branch="x", head="someone/homelab")
    renovate = run(2, actor=OTHER, event="pull_request", branch="renovate/x")
    get, asked = github([fork, renovate], [["made up", 0, 1]])
    sent: list[dict[str, Any]] = []
    state = ci_times.poll(get, sent.append, {})
    assert [len(names(body)) for body in sent] == [3, 3]  # run, job, step: no tests
    assert not [path for path in asked if "/annotations" in path]
    assert state["latest"] == {}


def test_metrics_are_the_latest_trusted_runs_by_kind_and_runner_and_drop_old_jobs() -> None:
    # Renovate's merge to main is reviewed code; Colin's pull request is his.
    merged = run(1, actor=OTHER)
    get, _ = github([merged, run(2, event="pull_request", branch="feature")])
    state = ci_times.poll(get, lambda body: None, {})
    now = ci_times.seconds("2026-11-13T00:00:00Z")
    text = ci_times.metrics(state["latest"], now)
    series = 'homelab_ci_job_seconds{{kind="{}",workflow="CI",ci_job="check",runner="vm"}} 30'
    assert series.format("main") in text and series.format("pr") in text
    assert "feature" not in text  # the branch is the trace's, not a series of its own
    assert ci_times.metrics(state["latest"], now + 31 * 86400).strip() == ""


def test_the_tests_slowest_list_fits_github_s_annotation_limit() -> None:
    import conftest

    conftest.TIMES.clear()
    conftest.TIMES.update({f"tests/test_x.py::test_{'y' * 140}[{n}]": [0.0, n] for n in range(99)})

    out = Path(tempfile.mkdtemp()) / "times.json"
    try:
        conftest.write_slowest(out)
    finally:
        conftest.TIMES.clear()
    rows = json.loads(out.read_text())
    assert len(out.read_text()) <= 3500 and rows[0][2] == 98
    note = {"title": ci_times.TESTS_TITLE, "message": out.read_text()}
    assert len(ci_times.slowest_tests(lambda path: [note], 1)) == len(rows)


def test_a_failed_request_keeps_what_was_sent_for_the_next_run() -> None:
    def get(path: str) -> dict[str, Any] | list[dict[str, Any]]:
        if path.endswith("/runs/2/attempts/1/jobs?per_page=100"):
            raise ConnectionResetError("dropped")
        return github([run(1), run(2)])[0](path)

    sent: list[dict[str, Any]] = []
    state = ci_times.poll(get, sent.append, {})
    assert state["sent"] == ["1/1"] and len(sent) == 1 and state["done"] is False


def test_a_run_alloy_refuses_is_skipped_and_a_whole_pass_says_done() -> None:
    def refuse(body: dict[str, Any]) -> None:
        if body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"] == ci_times.span_id(
            1, 1
        ):
            raise ci_times.urllib.error.HTTPError(
                "url", 413, "too big", email.message.Message(), None
            )

    get, _ = github([run(1), run(2)])
    state = ci_times.poll(get, refuse, {})
    assert state["sent"] == ["1/1", "2/1"] and state["done"] is True


def test_a_trace_holds_at_most_its_span_cap() -> None:
    many = job(10)
    many["steps"] = many["steps"] * 2000
    body = ci_times.trace(run(1), 1, [many], {})
    assert len(body["resourceSpans"][0]["scopeSpans"][0]["spans"]) == ci_times.SPANS_MAX


LOG = """\ufeff2026-11-12T00:00:11.1000000Z Current runner version: '2.338.2'
2026-11-12T00:00:11.9000000Z A job started hook has been configured by the runner administrator
2026-11-12T00:00:12.3000000Z ##[group]Run actions/checkout@abc
2026-11-12T00:00:14.0000000Z ##[group]Run mise run check
2026-11-12T00:00:14.1000000Z \x1b[34m[test]\x1b[0m \x1b[1m$ uv run python -m pytest tests/\x1b[0m
2026-11-12T00:00:14.1000000Z ^[[32m[lint]^[[0m $ uv run ruff check scripts/
2026-11-12T00:00:14.6000000Z [lint] $ shellcheck scripts/*.sh
2026-11-12T00:00:15.2000000Z [lint] Finished in 1.10s
2026-11-12T00:00:24.4000000Z [test] Finished in 10.30s
2026-11-12T00:00:24.5000000Z Post job cleanup.
2026-11-12T00:00:24.9000000Z Cleaning up orphan processes"""


def test_a_job_log_becomes_its_phases_to_the_millisecond() -> None:
    spans = {s["name"]: s for s in ci_times.log_spans("t", "job", LOG)}

    def took(name: str) -> float:
        span = spans[name]
        return round((int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])) / 1e9, 2)

    assert took("set up job") == 0.8
    assert took("job started hook") == 0.4
    assert took("step: actions/checkout@abc") == 1.7
    assert took("step: mise run check") == 10.5
    assert took("post job") == 0.4
    assert took("task: lint") == 1.1 and took("uv run ruff check scripts/") == 0.5
    assert took("shellcheck scripts/*.sh") == 0.6
    assert took("task: test") == 10.3
    # A task belongs to the step it ran in; a command to its task.
    assert spans["task: test"]["parentSpanId"] == spans["step: mise run check"]["spanId"]
    assert spans["shellcheck scripts/*.sh"]["parentSpanId"] == spans["task: lint"]["spanId"]


def test_only_colins_runs_have_their_logs_read() -> None:
    read: list[int] = []

    def logs(job_id: int) -> str:
        read.append(job_id)
        return LOG

    renovate = run(2, actor=OTHER, event="pull_request", branch="renovate/x")
    get, _ = github([run(1), renovate])
    sent: list[dict[str, Any]] = []
    ci_times.poll(get, sent.append, {}, logs=logs)
    assert read == [10]
    assert "task: test" in names(sent[0]) and "task: test" not in names(sent[1])


def test_a_bad_or_unfinished_log_still_gives_its_phases() -> None:
    # A run may print a carriage return and a line that looks like a time but is not one; a task
    # that failed never says "Finished in".
    log = LOG.replace("[test] Finished in 10.30s", "[test] FAILED")
    log += "\r2026-13-45T00:00:00Z not a time\n2026-11-12T00:00:25.0000000Z the end"
    spans = {s["name"]: s for s in ci_times.log_spans("t", "job", log)}
    assert spans["task: test"]["endTimeUnixNano"] == str(
        int(ci_times.log_time("2026-11-12T00:00:25") * 1e9)
    )
    assert {"key": "ended", "value": {"stringValue": "no"}} in spans["task: test"]["attributes"]


def serve(handler: type[http.server.BaseHTTPRequestHandler]) -> str:
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


def test_a_log_is_read_through_its_signed_link_without_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str | None] = []

    class Store(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"the log")

        def log_message(self, format: str, *args: object) -> None: ...

    store = serve(Store)
    codes = iter([302, 404, 500, 500])

    class GitHub(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(next(codes))
            self.send_header("Location", f"{store}/signed")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None: ...

    api = serve(GitHub)
    monkeypatch.setattr(ci_times, "API", api)
    monkeypatch.setattr(ci_times, "SIGNED", store)
    monkeypatch.setattr(ci_times.time, "sleep", lambda s: None)
    logs = ci_times.github_logs("secret")
    assert logs(1) == "the log" and seen == [None]
    assert logs(2) == ""  # gone
    with pytest.raises(OSError):
        logs(3)  # failing twice stops the pass
