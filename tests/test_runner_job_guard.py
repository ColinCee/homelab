import pytest

from scripts import runner_job_guard as guard

DEPLOY = "ColinCee/homelab/.github/workflows/deploy.yaml@refs/heads/main"
HEALTH = "ColinCee/homelab/.github/workflows/server-health.yaml@refs/heads/main"


@pytest.fixture(autouse=True)
def _no_journal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep test runs out of the host journal the guard writes to."""
    monkeypatch.setattr(guard, "_journal", lambda line: None)


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "GITHUB_REPOSITORY": "ColinCee/homelab",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": DEPLOY,
        "GITHUB_ACTOR": "ColinCee",
        "GITHUB_ACTOR_ID": "11246116",
        "GITHUB_TRIGGERING_ACTOR": "ColinCee",
    }
    env.update(overrides)
    return env


@pytest.mark.parametrize(
    "env",
    [
        _env(),
        _env(GITHUB_EVENT_NAME="schedule", GITHUB_WORKFLOW_REF=HEALTH),
        _env(
            GITHUB_ACTOR="renovate[bot]",
            GITHUB_ACTOR_ID="29139614",
            GITHUB_TRIGGERING_ACTOR="renovate[bot]",
        ),
        # A Renovate deploy that Colin re-runs.
        _env(
            GITHUB_ACTOR="renovate[bot]",
            GITHUB_ACTOR_ID="29139614",
            GITHUB_TRIGGERING_ACTOR="ColinCee",
        ),
    ],
)
def test_allows_the_deploy_workflows_on_main(env: dict[str, str]) -> None:
    assert guard.decide(env).allowed


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        (_env(GITHUB_EVENT_NAME="pull_request_target"), "event"),
        (_env(GITHUB_REF="refs/heads/feature"), "ref"),
        (
            _env(GITHUB_WORKFLOW_REF="ColinCee/homelab/.github/workflows/deploy.yaml@refs/heads/x"),
            "workflow",
        ),
        (
            _env(
                GITHUB_WORKFLOW_REF="ColinCee/homelab/.github/workflows/evil.yaml@refs/heads/main"
            ),
            "workflow",
        ),
        (_env(GITHUB_REPOSITORY="someone/homelab"), "repository"),
        (_env(GITHUB_ACTOR="mallory", GITHUB_ACTOR_ID="999"), "actor mallory"),
        (_env(GITHUB_ACTOR="ColinCee", GITHUB_ACTOR_ID="999"), "actor ColinCee (999)"),
        (_env(GITHUB_TRIGGERING_ACTOR="mallory"), "triggering actor"),
        (_env(BASH_ENV="/tmp/x"), "BASH_ENV"),
    ],
)
def test_refuses_everything_else(env: dict[str, str], reason: str) -> None:
    decision = guard.decide(env)

    assert not decision.allowed
    assert reason in decision.reason


@pytest.mark.parametrize(
    "missing",
    [
        "GITHUB_REPOSITORY",
        "GITHUB_EVENT_NAME",
        "GITHUB_REF",
        "GITHUB_WORKFLOW_REF",
        "GITHUB_ACTOR_ID",
        "GITHUB_TRIGGERING_ACTOR",
    ],
)
def test_a_missing_variable_is_a_refusal(missing: str) -> None:
    env = _env()
    del env[missing]

    assert not guard.decide(env).allowed


def test_enforce_stops_the_job_and_observe_lets_it_run(capsys: pytest.CaptureFixture[str]) -> None:
    bad = _env(GITHUB_EVENT_NAME="pull_request")

    stopped: list[int] = []

    def stop(pid: int) -> str:
        stopped.append(pid)
        return "killed Runner.Worker 42"

    assert guard.main(["--mode", "enforce"], bad, stop) == 1
    assert "::error::Runner guard refused" in capsys.readouterr().out
    assert len(stopped) == 1
    assert guard.main(["--mode", "observe"], bad, stop) == 0
    assert "::warning::" in capsys.readouterr().out
    assert guard.main([], _env(), stop) == 0
    assert len(stopped) == 1  # neither observe mode nor an allowed job stops anything


def test_audit_line_logs_other_variables_by_name_only() -> None:
    env = _env(ACTIONS_RUNTIME_TOKEN="secret-value")

    line = guard.audit_line(env, guard.decide(env), "observe")

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


def test_a_crafted_workflow_name_cannot_inject_a_workflow_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    nl = chr(10)
    crafted = f"ColinCee/homelab/.github/workflows/x{nl}::add-mask::y%.yaml@refs/heads/main"

    guard.main(["--mode", "enforce"], _env(GITHUB_WORKFLOW_REF=crafted), lambda pid: "stub")

    out = capsys.readouterr().out
    assert f"{nl}::add-mask::" not in out
    assert "%0A::add-mask::y%25" in out


PUBLIC = {"repo": "ColinCee/homelab", "public": True}


def _pr(source: str) -> dict:
    return {"pull_request": {"head": {"repo": {"full_name": source}}}}


@pytest.mark.parametrize(
    ("env", "event"),
    [
        (_env(), {}),
        (_env(GITHUB_EVENT_NAME="workflow_dispatch"), {}),
        (_env(GITHUB_EVENT_NAME="pull_request"), _pr("ColinCee/homelab")),
    ],
)
def test_a_public_repos_vm_takes_colins_own_jobs(env: dict[str, str], event: dict) -> None:
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
    env: dict[str, str], serves: dict, event: dict
) -> None:
    assert not guard.decide_vm(env, serves, event).allowed


def test_a_private_repos_vm_takes_any_job_of_its_repo_only() -> None:
    private = {"repo": "ColinCee/cortex", "public": False}
    renovate = _env(GITHUB_REPOSITORY="ColinCee/cortex", GITHUB_ACTOR="renovate[bot]")
    assert guard.decide_vm(renovate, private, {}).allowed
    assert guard.decide_vm(renovate | {"ENV": "production"}, private, {}).allowed
    assert not guard.decide_vm(_env(), private, {}).allowed


def test_the_vm_policy_reads_what_the_vm_serves_and_the_event(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serves, event = tmp_path / "serves.json", tmp_path / "event.json"
    serves.write_text('{"repo": "ColinCee/homelab", "public": true}')
    event.write_text('{"pull_request": {"head": {"repo": {"full_name": "someone/homelab"}}}}')
    monkeypatch.setattr(guard, "SERVES", str(serves))
    env = _env(GITHUB_EVENT_NAME="pull_request", GITHUB_EVENT_PATH=str(event))
    assert guard.main(["--policy", "vm"], env, lambda pid: "stub") == 1
    event.write_text('{"pull_request": {"head": {"repo": {"full_name": "ColinCee/homelab"}}}}')
    assert guard.main(["--policy", "vm"], env, lambda pid: "stub") == 0
