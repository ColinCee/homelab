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
    }
    env.update(overrides)
    return env


@pytest.mark.parametrize(
    "env",
    [
        _env(),
        _env(GITHUB_EVENT_NAME="schedule", GITHUB_WORKFLOW_REF=HEALTH),
        _env(GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_WORKFLOW_REF=HEALTH),
    ],
)
def test_allows_the_deploy_workflows_on_main(env: dict[str, str]) -> None:
    assert guard.decide(env).allowed


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        (_env(GITHUB_EVENT_NAME="pull_request", GITHUB_REF="refs/pull/7/merge"), "event"),
        (_env(GITHUB_EVENT_NAME="pull_request_target"), "event"),
        (_env(GITHUB_EVENT_NAME="workflow_run"), "event"),
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
        (_env(BASH_ENV="/tmp/x"), "BASH_ENV"),
        (_env(ENV="/tmp/x"), "ENV"),
    ],
)
def test_refuses_everything_else(env: dict[str, str], reason: str) -> None:
    decision = guard.decide(env)

    assert not decision.allowed
    assert reason in decision.reason


@pytest.mark.parametrize(
    "missing", ["GITHUB_REPOSITORY", "GITHUB_EVENT_NAME", "GITHUB_REF", "GITHUB_WORKFLOW_REF"]
)
def test_a_missing_variable_is_a_refusal(missing: str) -> None:
    env = _env()
    del env[missing]

    assert not guard.decide(env).allowed


def test_enforce_fails_the_job_and_observe_lets_it_run(capsys: pytest.CaptureFixture[str]) -> None:
    bad = _env(GITHUB_EVENT_NAME="pull_request")

    assert guard.main(["--mode", "enforce"], bad) == 1
    assert "::error::Runner guard refused" in capsys.readouterr().out
    assert guard.main(["--mode", "observe"], bad) == 0
    assert "::warning::" in capsys.readouterr().out
    assert guard.main([], _env()) == 0


def test_audit_line_logs_other_variables_by_name_only() -> None:
    env = _env(ACTIONS_RUNTIME_TOKEN="secret-value")

    line = guard.audit_line(env, guard.decide(env), "observe")

    assert "ACTIONS_RUNTIME_TOKEN" in line
    assert "secret-value" not in line
    assert "verdict=allow" in line
