"""CI run times (scripts/ci_times.py): one trace per run attempt, the latest of Colin's own runs as
metrics, and others' runs' own words kept out."""

import json

from scripts import ci_times

COLIN = {"id": ci_times.OWNER_ID, "login": "ColinCee"}
OTHER = {"id": 1, "login": "someone"}


def run(n: int, *, actor=COLIN, event="push", branch="main", head=ci_times.REPO, attempt=1) -> dict:
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


def job(n: int, labels=("self-hosted", "homelab-fc")) -> dict:
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


def github(runs: list[dict], tests: list[dict] | None = None):
    asked: list[str] = []

    def get(path: str):
        asked.append(path)
        if "/annotations" in path:
            message = json.dumps(tests or [])
            return [{"title": ci_times.TESTS_TITLE, "message": message}]
        if path.endswith("/jobs?per_page=100"):
            return {"jobs": [job(int(path.split("/runs/")[1].split("/")[0]) * 10)]}
        return {"workflow_runs": runs}

    return get, asked


def names(body: dict) -> list[str]:
    return [s["name"] for s in body["resourceSpans"][0]["scopeSpans"][0]["spans"]]


def test_each_run_attempt_is_sent_once_with_its_jobs_steps_and_tests() -> None:
    tests = [{"test": "tests/test_x.py::test_slow", "start": 1794441625.0, "seconds": 3.0}]
    get, _ = github([run(1), run(1, attempt=2)], tests)
    sent: list[dict] = []
    state = ci_times.poll(get, sent.append, {}, 0.0)
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
    again: list[dict] = []
    ci_times.poll(get, again.append, state, 0.0)
    assert again == []


def test_a_test_span_stays_inside_its_step_whatever_its_clock_says() -> None:
    late = [{"test": "t", "start": 9e9, "seconds": 5.0}]
    get, _ = github([run(1)], late)
    sent: list[dict] = []
    ci_times.poll(get, sent.append, {}, 0.0)
    spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    step, test = spans[2], spans[3]
    assert step["startTimeUnixNano"] <= test["startTimeUnixNano"] <= step["endTimeUnixNano"]
    assert test["endTimeUnixNano"] <= step["endTimeUnixNano"]


def test_others_runs_are_traced_without_their_tests_or_metrics() -> None:
    fork = run(1, event="pull_request", branch="x", head="someone/homelab")
    renovate = run(2, actor=OTHER, event="pull_request", branch="renovate/x")
    get, asked = github([fork, renovate], [{"test": "made up", "start": 0, "seconds": 1}])
    sent: list[dict] = []
    state = ci_times.poll(get, sent.append, {}, 0.0)
    assert [len(names(body)) for body in sent] == [3, 3]  # run, job, step: no tests
    assert not [path for path in asked if "/annotations" in path]
    assert state["latest"] == {}


def test_metrics_are_the_latest_of_colins_runs_by_kind_and_runner() -> None:
    get, _ = github([run(1), run(2, event="pull_request", branch="feature")])
    state = ci_times.poll(get, lambda body: None, {}, 0.0)
    text = ci_times.metrics(state["latest"])
    assert 'homelab_ci_job_seconds{kind="main",workflow="CI",job="check",runner="vm"} 30' in text
    assert 'homelab_ci_job_seconds{kind="pr",workflow="CI",job="check",runner="vm"} 30' in text
    assert "feature" not in text  # the branch is the trace's, not a series of its own


def test_a_failed_request_keeps_what_was_sent_for_the_next_run() -> None:
    def get(path: str):
        if path.endswith("/runs/2/attempts/1/jobs?per_page=100"):
            raise ConnectionResetError("dropped")
        return github([run(1), run(2)])[0](path)

    sent: list[dict] = []
    state = ci_times.poll(get, sent.append, {}, 0.0)
    assert state["sent"] == ["1/1"] and len(sent) == 1
