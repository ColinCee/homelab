from pathlib import Path
from typing import Any

import pytest

from scripts import runner_job_guard as guard


@pytest.fixture(autouse=True)
def _no_journal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep test runs out of the journal the guard writes to."""
    monkeypatch.setattr(guard, "_journal", lambda line: None)


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "GITHUB_REPOSITORY": "ColinCee/homelab",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_ACTOR": "ColinCee",
        "GITHUB_ACTOR_ID": "11246116",
        "GITHUB_TRIGGERING_ACTOR": "ColinCee",
    }
    env.update(overrides)
    return env


@pytest.fixture
def serves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "serves.json"
    path.write_text('{"repo": "ColinCee/homelab", "public": true}')
    monkeypatch.setattr(guard, "SERVES", str(path))
    return path


def test_a_refused_job_is_stopped_and_an_allowed_one_is_not(
    serves: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stopped: list[int] = []

    def stop(pid: int) -> str:
        stopped.append(pid)
        return "killed Runner.Worker 42"

    assert guard.main(_env(GITHUB_ACTOR="mallory", GITHUB_ACTOR_ID="999"), stop) == 1
    assert "::error::Runner guard refused" in capsys.readouterr().out
    assert guard.main(_env(), stop) == 0
    assert len(stopped) == 1


def test_audit_line_logs_other_variables_by_name_only() -> None:
    env = _env(ACTIONS_RUNTIME_TOKEN="secret-value")

    line = guard.audit_line(env, guard.decide_vm(env, PUBLIC, {}))

    assert "ACTIONS_RUNTIME_TOKEN" in line
    assert "secret-value" not in line
    assert "verdict=allow" in line


# A process tree as the runner builds it: Runner.Listener -> Runner.Worker -> hook.
TREE = {300: ("python3", 200), 200: ("Runner.Worker", 100), 100: ("Runner.Listener", 1)}


def test_finds_the_worker_above_the_hook() -> None:
    assert guard.find_worker(300, TREE.__getitem__) == 200


def test_no_worker_above_the_hook_is_none() -> None:
    tree = {300: ("python3", 100), 100: ("bash", 1)}

    assert guard.find_worker(300, tree.__getitem__) is None


def test_a_vanished_process_is_none() -> None:
    def gone(pid: int) -> tuple[str, int]:
        raise FileNotFoundError(pid)

    assert guard.find_worker(300, gone) is None


def test_stop_job_kills_only_the_worker() -> None:
    killed: list[int] = []

    outcome = guard.stop_job(300, TREE.__getitem__, killed.append)

    assert killed == [200]
    assert outcome == "killed Runner.Worker 200"


def test_stop_job_reports_a_failed_kill() -> None:
    def refuse(pid: int) -> None:
        raise PermissionError("not permitted")

    assert "could not kill" in guard.stop_job(300, TREE.__getitem__, refuse)


def test_a_crafted_repository_name_cannot_inject_a_workflow_command(
    serves: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nl = chr(10)

    guard.main(_env(GITHUB_REPOSITORY=f"x{nl}::add-mask::y%"), lambda pid: "stub")

    out = capsys.readouterr().out
    assert f"{nl}::add-mask::" not in out
    assert "%0A::add-mask::y%25" in out


PUBLIC = {"repo": "ColinCee/homelab", "public": True}


def _pr(source: str) -> dict[str, Any]:
    return {"pull_request": {"head": {"repo": {"full_name": source}}}}


@pytest.mark.parametrize(
    ("env", "event"),
    [
        (_env(), {}),
        (_env(GITHUB_EVENT_NAME="workflow_dispatch"), {}),
        (_env(GITHUB_EVENT_NAME="pull_request"), _pr("ColinCee/homelab")),
    ],
)
def test_a_public_repos_vm_takes_colins_own_jobs(
    env: dict[str, str], event: dict[str, Any]
) -> None:
    assert guard.decide_vm(env, PUBLIC, event).allowed


@pytest.mark.parametrize(
    ("env", "serves", "event"),
    [
        (_env(GITHUB_EVENT_NAME="pull_request"), PUBLIC, _pr("someone/homelab")),  # a fork
        (_env(GITHUB_EVENT_NAME="pull_request"), PUBLIC, {}),  # no event file
        (_env(GITHUB_ACTOR="renovate[bot]", GITHUB_ACTOR_ID="29139614"), PUBLIC, {}),
        (_env(GITHUB_TRIGGERING_ACTOR="someone"), PUBLIC, {}),
        (_env(GITHUB_EVENT_NAME="pull_request_target"), PUBLIC, _pr("ColinCee/homelab")),
        (_env(GITHUB_REPOSITORY="ColinCee/other"), PUBLIC, {}),
        (_env(BASH_ENV="/tmp/x"), PUBLIC, {}),
        (_env(), {}, {}),  # serves.json missing
        # Visibility unknown counts as public.
        (
            _env(GITHUB_ACTOR="renovate[bot]", GITHUB_ACTOR_ID="29139614"),
            {"repo": "ColinCee/homelab"},
            {},
        ),
    ],
)
def test_a_public_repos_vm_refuses_every_other_job(
    env: dict[str, str], serves: dict[str, Any], event: dict[str, Any]
) -> None:
    assert not guard.decide_vm(env, serves, event).allowed


def test_a_private_repos_vm_takes_any_job_of_its_repo_only() -> None:
    private = {"repo": "ColinCee/cortex", "public": False}
    renovate = _env(GITHUB_REPOSITORY="ColinCee/cortex", GITHUB_ACTOR="renovate[bot]")
    assert guard.decide_vm(renovate, private, {}).allowed
    assert guard.decide_vm(renovate | {"ENV": "production"}, private, {}).allowed
    assert not guard.decide_vm(_env(), private, {}).allowed


def test_the_guard_reads_what_the_vm_serves_and_the_event(serves: Path, tmp_path: Path) -> None:
    event = tmp_path / "event.json"
    event.write_text('{"pull_request": {"head": {"repo": {"full_name": "someone/homelab"}}}}')
    env = _env(GITHUB_EVENT_NAME="pull_request", GITHUB_EVENT_PATH=str(event))
    assert guard.main(env, lambda pid: "stub") == 1
    event.write_text('{"pull_request": {"head": {"repo": {"full_name": "ColinCee/homelab"}}}}')
    assert guard.main(env, lambda pid: "stub") == 0
