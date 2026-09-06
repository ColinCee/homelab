import subprocess
from pathlib import Path

import pytest

from scripts import deploy


def _make_stack(repo_root: Path, name: str, *, with_env: bool = False) -> Path:
    stack_dir = repo_root / "stacks" / name
    stack_dir.mkdir(parents=True)
    (stack_dir / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    if with_env:
        (stack_dir / ".env.example").write_text("TOKEN=${TOKEN}\n", encoding="utf-8")
        (stack_dir / ".env").write_text("TOKEN='value'\n", encoding="utf-8")
    return stack_dir


def _recording_runner(fail_when: str | None = None):
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        if fail_when and fail_when in command and "up" in command:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    return runner, calls


def test_discovers_stacks_and_stack_prefixed_systemd_pairs(tmp_path: Path) -> None:
    repo_root = tmp_path
    alpha = _make_stack(repo_root, "alpha")
    _make_stack(repo_root, "beta")
    systemd_dir = alpha / "systemd"
    systemd_dir.mkdir()
    (systemd_dir / "alpha-backup.service").write_text("[Service]\n", encoding="utf-8")
    (systemd_dir / "alpha-backup.timer").write_text("[Timer]\n", encoding="utf-8")

    plans = deploy.discover_stack_plans(repo_root)

    assert [plan.name for plan in plans] == ["alpha", "beta"]
    assert plans[0].timers == (systemd_dir / "alpha-backup.timer",)


@pytest.mark.parametrize(
    ("units", "message"),
    [
        (["backup.service", "backup.timer"], "must start"),
        (["alpha-backup.service"], "both"),
        (["alpha-backup.timer"], "both"),
    ],
)
def test_rejects_unpaired_or_unprefixed_systemd_units(
    tmp_path: Path, units: list[str], message: str
) -> None:
    stack_dir = _make_stack(tmp_path, "alpha")
    systemd_dir = stack_dir / "systemd"
    systemd_dir.mkdir()
    for unit in units:
        (systemd_dir / unit).write_text("", encoding="utf-8")

    with pytest.raises(deploy.DeploymentInputError, match=message):
        deploy.discover_stack_plans(tmp_path)


def test_validates_every_stack_before_building_any(tmp_path: Path) -> None:
    _make_stack(tmp_path, "alpha", with_env=True)
    _make_stack(tmp_path, "beta")
    runner, calls = _recording_runner()
    plans = deploy.discover_stack_plans(tmp_path)

    report = deploy.deploy(tmp_path, plans, runner=runner)

    assert report.failed == {}
    config_indexes = [index for index, (command, _) in enumerate(calls) if "config" in command]
    build_indexes = [index for index, (command, _) in enumerate(calls) if "build" in command]
    assert config_indexes and build_indexes
    assert max(config_indexes) < min(build_indexes)


def test_compose_validation_failure_prevents_all_mutations(tmp_path: Path) -> None:
    _make_stack(tmp_path, "alpha")
    _make_stack(tmp_path, "beta")
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if "config" in command:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    plans = deploy.discover_stack_plans(tmp_path)
    with pytest.raises(deploy.DeploymentInputError, match="Compose validation failed"):
        deploy.deploy(tmp_path, plans, runner=runner)

    assert all("build" not in command and "up" not in command for command in calls)
    assert len(calls) == 2


def test_deployment_does_not_forward_shell_secrets_to_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_stack(tmp_path, "sample")
    monkeypatch.setenv("SAMPLE_SECRET", "not-forwarded")
    runner, calls = _recording_runner()
    plans = deploy.discover_stack_plans(tmp_path)

    deploy.deploy(tmp_path, plans, runner=runner)

    for _command, kwargs in calls:
        environment = kwargs.get("env")
        if isinstance(environment, dict):
            assert "not-forwarded" not in environment.values()


def test_builds_all_profiles_but_does_not_start_profile_jobs(tmp_path: Path) -> None:
    _make_stack(tmp_path, "sample")
    runner, calls = _recording_runner()
    plans = deploy.discover_stack_plans(tmp_path)

    deploy.deploy(tmp_path, plans, runner=runner)

    config = next(command for command, _ in calls if "config" in command)
    build = next(command for command, _ in calls if "build" in command)
    up = next(command for command, _ in calls if "up" in command)
    assert "--profile" in config and "*" in config
    assert "--profile" in build and "*" in build
    assert "--profile" not in up
    assert "run" not in up
    up_call = next(kwargs for command, kwargs in calls if command == up)
    environment = up_call["env"]
    assert isinstance(environment, dict)
    assert environment["COMPOSE_PROFILES"] == ""


def test_failed_stack_is_reported_while_other_stacks_continue(tmp_path: Path) -> None:
    _make_stack(tmp_path, "alpha")
    _make_stack(tmp_path, "beta")
    runner, _calls = _recording_runner(str(tmp_path / "stacks" / "alpha" / "compose.yaml"))
    plans = deploy.discover_stack_plans(tmp_path)

    report = deploy.deploy(tmp_path, plans, runner=runner)

    assert report.completed == ("beta",)
    assert report.failed == {"alpha": "command exited with 1"}
    assert report.partial


def test_installs_and_enables_discovered_timer_pairs(tmp_path: Path) -> None:
    stack_dir = _make_stack(tmp_path, "alpha")
    systemd_dir = stack_dir / "systemd"
    systemd_dir.mkdir()
    (systemd_dir / "alpha-backup.service").write_text("[Service]\n", encoding="utf-8")
    (systemd_dir / "alpha-backup.timer").write_text("[Timer]\n", encoding="utf-8")
    unit_dir = tmp_path / "user-systemd"
    runner, calls = _recording_runner()
    plans = deploy.discover_stack_plans(tmp_path)

    deploy.deploy(tmp_path, plans, unit_dir=unit_dir, runner=runner)

    assert (unit_dir / "alpha-backup.service").read_text() == "[Service]\n"
    assert (unit_dir / "alpha-backup.timer").read_text() == "[Timer]\n"
    assert any(
        command == ["systemctl", "--user", "enable", "--now", "alpha-backup.timer"]
        for command, _ in calls
    )
