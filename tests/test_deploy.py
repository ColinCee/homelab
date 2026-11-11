import functools
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from scripts import deploy


@functools.cache
def docker() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0


@pytest.fixture(autouse=True)
def _no_github_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)


def _make_stack(repo_root: Path, name: str, *, with_env: bool = False) -> Path:
    stack_dir = repo_root / "stacks" / name
    stack_dir.mkdir(parents=True)
    (stack_dir / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    if with_env:
        (stack_dir / "settings.env").write_text("SETTING=value\n", encoding="utf-8")
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


def test_builds_all_profiles_but_starts_only_those_its_env_turns_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMPOSE_PROFILES", "from-the-shell")
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
    assert "COMPOSE_PROFILES" not in environment


@pytest.mark.skipif("not docker()", reason="needs Docker")
@pytest.mark.parametrize(("profiles", "started"), [("", ["app"]), ("extra", ["app", "extra"])])
def test_a_profile_in_the_stacks_env_reaches_up(
    tmp_path: Path, profiles: str, started: list[str]
) -> None:
    stack_dir = tmp_path / "stacks" / "sample"
    stack_dir.mkdir(parents=True)
    (stack_dir / "compose.yaml").write_text(
        "services:\n  app: {image: x}\n  extra: {image: x, profiles: [extra]}\n", encoding="utf-8"
    )
    (stack_dir / "settings.env").write_text(f"COMPOSE_PROFILES={profiles}\n", encoding="utf-8")
    [plan] = deploy.discover_stack_plans(tmp_path)

    listed = subprocess.run(
        deploy.compose_command(plan, "config", "--services"),
        env=deploy._compose_environment(),
        capture_output=True,
        text=True,
        check=True,
    )
    assert sorted(listed.stdout.split()) == started


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


def test_config_hash_tracks_committed_config_and_reaches_up(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    stack_dir = _make_stack(tmp_path, "sample")
    (stack_dir / "app.conf").write_text("a\n", encoding="utf-8")
    git("init", "-q")
    git("add", ".")
    before = deploy.stack_config_hash(tmp_path, stack_dir)

    (stack_dir / "data").mkdir()
    (stack_dir / "data" / "state.db").write_text("runtime\n", encoding="utf-8")
    (stack_dir / "compose.yaml").write_text("services: {x: {}}\n", encoding="utf-8")
    assert deploy.stack_config_hash(tmp_path, stack_dir) == before

    (stack_dir / "app.conf").write_text("b\n", encoding="utf-8")
    after = deploy.stack_config_hash(tmp_path, stack_dir)
    assert after != before

    runner, calls = _recording_runner()
    deploy.deploy(tmp_path, deploy.discover_stack_plans(tmp_path), runner=runner)

    up_env = next(kwargs["env"] for command, kwargs in calls if "up" in command)
    assert isinstance(up_env, dict)
    assert up_env["STACK_CONFIG_HASH"] == after


def test_a_changed_secret_changes_the_hash_but_its_value_does_not_reach_it(
    tmp_path: Path,
) -> None:
    stack_dir = _make_stack(tmp_path, "sample")
    (stack_dir / "compose.yaml").write_text(
        "services: {}\nsecrets:\n  token:\n    file: ../../secrets/token\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    with pytest.raises(deploy.DeploymentInputError, match="token; render it"):
        deploy.discover_stack_plans(tmp_path)

    secret = tmp_path / "secrets" / "token"
    secret.parent.mkdir()
    secret.write_text("a", encoding="utf-8")
    os.utime(secret, (1, 1))
    first = deploy.stack_config_hash(tmp_path, stack_dir)
    secret.write_text("b", encoding="utf-8")
    os.utime(secret, (1, 1))
    assert deploy.stack_config_hash(tmp_path, stack_dir) == first
    os.utime(secret, (2, 2))
    assert deploy.stack_config_hash(tmp_path, stack_dir) != first


def _git(repo_root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit_all(repo_root: Path) -> str:
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-qm", "change")
    return _git(repo_root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    for name in ("gateway", "observability"):
        _make_stack(tmp_path, name)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "deploy.py").write_text("", encoding="utf-8")
    _commit_all(tmp_path)
    return tmp_path


def test_a_change_to_one_stack_selects_only_that_stack(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    (repo / "stacks" / "gateway" / "compose.yaml").write_text(
        "services: {app: {image: new}}\n", encoding="utf-8"
    )
    _commit_all(repo)

    assert deploy.changed_stacks(repo, before) == ("gateway",)


def test_a_change_to_shared_deploy_files_selects_all_stacks(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    (repo / "scripts" / "deploy.py").write_text("# changed\n", encoding="utf-8")
    (repo / "stacks" / "observability" / "extra.txt").write_text("x\n", encoding="utf-8")
    _commit_all(repo)

    assert deploy.changed_stacks(repo, before) is None


@pytest.mark.parametrize("since", ["", "0123456789abcdef0123456789abcdef01234567"])
def test_a_missing_or_unknown_last_deploy_selects_all_stacks(repo: Path, since: str) -> None:
    assert deploy.changed_stacks(repo, since) is None


def test_deploys_changed_stacks_and_records_the_commit(
    repo: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path_factory.mktemp("state") / "last-deployed"
    output = state.with_name("github-output")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    state.write_text(_git(repo, "rev-parse", "HEAD") + "\n", encoding="utf-8")
    (repo / "stacks" / "gateway" / "compose.yaml").write_text(
        "services: {app: {image: new}}\n", encoding="utf-8"
    )
    head = _commit_all(repo)
    runner, calls = _recording_runner()

    assert deploy.run(repo, (), last_deployed=state, runner=runner) == 0

    composed = {command[command.index("-f") + 1] for command, _ in calls if "-f" in command}
    assert composed == {str(repo / "stacks" / "gateway" / "compose.yaml")}
    assert state.read_text(encoding="utf-8").strip() == head
    deployed, ready = output.read_text(encoding="utf-8").splitlines()
    assert deployed == 'deployed=["gateway"]'
    ready_at = json.loads(ready.removeprefix("ready_at="))
    assert list(ready_at) == ["gateway"]
    assert datetime.fromisoformat(ready_at["gateway"]).tzinfo is not None


def test_a_failed_deploy_keeps_the_old_marker_so_the_next_retries_in_full(
    repo: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    state = tmp_path_factory.mktemp("state") / "last-deployed"
    before = _git(repo, "rev-parse", "HEAD")
    state.write_text(before + "\n", encoding="utf-8")
    (repo / "scripts" / "deploy.py").write_text("# changed\n", encoding="utf-8")
    _commit_all(repo)
    runner, _ = _recording_runner(fail_when="up")

    assert deploy.run(repo, (), last_deployed=state, runner=runner) == 1

    assert state.read_text(encoding="utf-8").strip() == before


# What `docker compose config --format json` reports for each stack's networks and volumes.
_COMPOSE_CONFIG = {
    "backup": {"volumes": {"data": {"name": "observability_data", "external": True}}},
    "gateway": {
        "networks": {
            "default": {"name": "gateway_default"},
            "shared": {"name": "shared"},
        },
    },
    "observability": {
        "networks": {"shared": {"name": "shared", "external": True}},
        "volumes": {"data": {"name": "observability_data"}},
    },
    "web": {},
}


def _describing_runner():
    runner, calls = _recording_runner()

    def describe(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if "--format" in command:
            stack = Path(command[command.index("-f") + 1]).parent.name
            return subprocess.CompletedProcess(command, 0, json.dumps(_COMPOSE_CONFIG[stack]))
        return runner(command, **kwargs)

    return describe, calls


@pytest.fixture
def stacks(repo: Path) -> Path:
    for name in ("backup", "web"):
        _make_stack(repo, name)
    _commit_all(repo)
    return repo


def _change(repo_root: Path, *names: str) -> str:
    before = _git(repo_root, "rev-parse", "HEAD")
    for name in names:
        (repo_root / "stacks" / name / "extra.txt").write_text("x\n", encoding="utf-8")
    _commit_all(repo_root)
    return before


def _up_order(calls) -> list[str]:
    return [Path(c[c.index("-f") + 1]).parent.name for c, _ in calls if "up" in c]


def test_a_full_deploy_puts_the_pushs_changed_stack_first(stacks: Path) -> None:
    before = _change(stacks, "web")
    runner, calls = _describing_runner()

    assert deploy.run(stacks, (), first_changed_since=before, runner=runner) == 0

    assert _up_order(calls) == ["web", "backup", "gateway", "observability"]


def test_a_changed_stack_waits_for_the_stacks_creating_what_it_uses(stacks: Path) -> None:
    # On a fresh host observability's external network exists only once the gateway
    # creates it, and backup's external volume only once observability does.
    before = _change(stacks, "backup", "observability")
    runner, calls = _describing_runner()

    assert deploy.run(stacks, (), first_changed_since=before, runner=runner) == 0

    assert _up_order(calls) == ["gateway", "observability", "backup", "web"]


@pytest.mark.parametrize("since", ["", "0" * 40])
def test_an_unknown_before_commit_keeps_the_name_order(stacks: Path, since: str) -> None:
    runner, calls = _describing_runner()

    assert deploy.run(stacks, (), first_changed_since=since, runner=runner) == 0

    assert _up_order(calls) == ["backup", "gateway", "observability", "web"]
