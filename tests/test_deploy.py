import functools
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import git, services

from stacks.deploy import deploy


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


# Each command and the keyword arguments it was run with, as subprocess.run takes them.
type Calls = list[tuple[list[str], dict[str, Any]]]


def _recording_runner(fail_when: str | None = None) -> tuple[deploy.CommandRunner, Calls]:
    calls: Calls = []

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
    for unit in ("alpha-ring.service", "alpha-ring.path", "alpha-ring.timer"):
        (systemd_dir / unit).write_text("", encoding="utf-8")

    plans = deploy.discover_stack_plans(repo_root)

    assert [plan.name for plan in plans] == ["alpha", "beta"]
    assert plans[0].triggers == (
        systemd_dir / "alpha-backup.timer",
        systemd_dir / "alpha-ring.path",
        systemd_dir / "alpha-ring.timer",
    )


@pytest.mark.parametrize(
    ("units", "message"),
    [
        (["backup.service", "backup.timer"], "must start"),
        (["alpha-backup.service"], "needs a .timer or .path"),
        (["alpha-backup.timer"], "needs a .service"),
        (["alpha-backup.path"], "needs a .service"),
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


def test_the_repositorys_own_stacks_pass_the_deploys_unit_checks() -> None:
    # Else deploy-main refuses every stack at once, which no fake stack above shows.
    root = Path(__file__).parent.parent
    for stack in (root / "stacks").iterdir():
        deploy.discover_triggers(stack)


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


def test_records_the_services_to_keep_running_and_each_timers_stamp(tmp_path: Path) -> None:
    stack_dir = _make_stack(tmp_path, "alpha")
    systemd_dir = stack_dir / "systemd"
    systemd_dir.mkdir()
    for name in ("alpha-upkeep", "alpha-fresh"):
        (systemd_dir / f"{name}.service").write_text("[Service]\n", encoding="utf-8")
        (systemd_dir / f"{name}.timer").write_text("[Timer]\n", encoding="utf-8")
    stamps = tmp_path / "stamps"
    stamps.mkdir()
    (stamps / "stamp-alpha-upkeep.timer").touch()
    os.utime(stamps / "stamp-alpha-upkeep.timer", (1794000000, 1794000000))
    metrics = tmp_path / "metrics"
    metrics.mkdir()
    config = {
        "name": "alpha",
        "services": {
            "web": {"restart": "unless-stopped"},
            "db": {"restart": "always"},
            "once": {"restart": "no"},
            "migrate": {},
        },
    }
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        out = json.dumps(config).encode() if "json" in command else b""
        return subprocess.CompletedProcess(command, 0, stdout=out)

    report = deploy.deploy(
        tmp_path,
        deploy.discover_stack_plans(tmp_path),
        unit_dir=tmp_path / "units",
        metrics_dir=metrics,
        stamp_dir=stamps,
        runner=runner,
    )

    assert report.completed == ("alpha",)
    # Read with the stack's own profiles, as `up` started them.
    assert "--profile" not in next(c for c in calls if "json" in c)
    samples = [
        line
        for line in (metrics / "deploy-alpha.prom").read_text().splitlines()
        if not line.startswith("#")
    ]
    assert samples == [
        'homelab_container_expected{project="alpha",service="db"} 1',
        'homelab_container_expected{project="alpha",service="web"} 1',
        'homelab_timer_last_trigger_timestamp_seconds{timer="alpha-upkeep"} 1794000000',
    ]
    assert [p.name for p in metrics.iterdir()] == ["deploy-alpha.prom"]


TWO_SERVICES = """services:
  app: {volumes: [./app.conf:/etc/app.conf:ro, ./data:/data], secrets: [token]}
  web: {volumes: [./web:/etc/web:ro]}
secrets:
  token: {file: ../../secrets/token}
"""


def test_a_service_s_hash_follows_only_the_tracked_files_and_secrets_it_mounts(
    tmp_path: Path,
) -> None:
    stack_dir = _make_stack(tmp_path, "sample")
    (stack_dir / "compose.yaml").write_text(TWO_SERVICES, encoding="utf-8")
    (stack_dir / "app.conf").write_text("a\n", encoding="utf-8")
    (stack_dir / "web").mkdir()
    (stack_dir / "web" / "site.conf").write_text("a\n", encoding="utf-8")
    secret = tmp_path / "secrets" / "token"
    secret.parent.mkdir()
    secret.write_text("a", encoding="utf-8")
    os.utime(secret, (1, 1))
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "stacks")
    before = deploy.config_hashes(tmp_path, stack_dir)
    assert set(before) == {"CONFIG_HASH_APP", "CONFIG_HASH_WEB"}

    # Runtime data (untracked) and a secret's value change nothing.
    (stack_dir / "data").mkdir()
    (stack_dir / "data" / "state.db").write_text("runtime\n", encoding="utf-8")
    secret.write_text("b", encoding="utf-8")
    os.utime(secret, (1, 1))
    assert deploy.config_hashes(tmp_path, stack_dir) == before

    # A file in web's folder changes web's hash alone; a newer secret, app's alone.
    (stack_dir / "web" / "site.conf").write_text("b\n", encoding="utf-8")
    after = deploy.config_hashes(tmp_path, stack_dir)
    assert after["CONFIG_HASH_WEB"] != before["CONFIG_HASH_WEB"]
    assert after["CONFIG_HASH_APP"] == before["CONFIG_HASH_APP"]
    os.utime(secret, (2, 2))
    assert deploy.config_hashes(tmp_path, stack_dir)["CONFIG_HASH_APP"] != after["CONFIG_HASH_APP"]

    runner, calls = _recording_runner()
    deploy.deploy(tmp_path, deploy.discover_stack_plans(tmp_path), runner=runner)
    up_env = next(kwargs["env"] for command, kwargs in calls if "up" in command)
    assert isinstance(up_env, dict)
    assert up_env["CONFIG_HASH_WEB"] == after["CONFIG_HASH_WEB"]

    # A shared file mounted from beside the stack changes the hash of the service mounting it.
    shared = tmp_path / "stacks" / "telemetry" / "telemetry.py"
    shared.parent.mkdir()
    shared.write_text("a\n", encoding="utf-8")
    git(tmp_path, "add", "stacks")
    compose = TWO_SERVICES.replace(
        "./web:/etc/web:ro", "./web:/etc/web:ro, ../telemetry/telemetry.py:/t.py:ro"
    )
    (stack_dir / "compose.yaml").write_text(compose, encoding="utf-8")
    mounted = deploy.config_hashes(tmp_path, stack_dir)
    shared.write_text("b\n", encoding="utf-8")
    assert (
        deploy.config_hashes(tmp_path, stack_dir)["CONFIG_HASH_WEB"] != mounted["CONFIG_HASH_WEB"]
    )


def test_each_service_reading_files_or_secrets_has_a_label_with_its_own_hash() -> None:
    # Without the label a changed file never restarts the service; with another's hash it restarts
    # on the wrong files. Home Assistant's config/ is its own runtime data, which it reloads itself.
    for stack, name, spec in services():
        label = (spec.get("labels") or {}).get("homelab.config-hash")
        reads = bool(deploy._bind_sources(spec) or spec.get("secrets"))
        if (reads and name != "homeassistant") or label is not None:
            assert label == "${" + deploy.hash_variable(name) + ":-}", (stack, name)


def test_a_secret_mounted_but_missing_stops_the_deploy(tmp_path: Path) -> None:
    stack_dir = _make_stack(tmp_path, "sample")
    (stack_dir / "compose.yaml").write_text(
        "services: {}\nsecrets:\n  token:\n    file: ../../secrets/token\n", encoding="utf-8"
    )
    with pytest.raises(deploy.DeploymentInputError, match="token; copy it from Bitwarden"):
        deploy.discover_stack_plans(tmp_path)


def _commit_all(repo_root: Path) -> str:
    git(repo_root, "add", "-A")
    git(repo_root, "commit", "-qm", "change")
    return git(repo_root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    for name in ("gateway", "observability"):
        _make_stack(tmp_path, name)
    (tmp_path / "stacks" / "deploy").mkdir()
    (tmp_path / "stacks" / "deploy" / "deploy.py").write_text("", encoding="utf-8")
    _commit_all(tmp_path)
    return tmp_path


def test_a_change_to_one_stack_selects_only_that_stack(repo: Path) -> None:
    before = git(repo, "rev-parse", "HEAD")
    (repo / "stacks" / "gateway" / "compose.yaml").write_text(
        "services: {app: {image: new}}\n", encoding="utf-8"
    )
    _commit_all(repo)

    assert deploy.changed_stacks(repo, before) == ("gateway",)


def test_a_change_to_shared_deploy_files_selects_all_stacks(repo: Path) -> None:
    before = git(repo, "rev-parse", "HEAD")
    (repo / "stacks" / "deploy" / "deploy.py").write_text("# changed\n", encoding="utf-8")
    (repo / "stacks" / "observability" / "extra.txt").write_text("x\n", encoding="utf-8")
    _commit_all(repo)

    assert deploy.changed_stacks(repo, before) is None


def test_a_change_to_a_shared_folder_under_stacks_selects_all_stacks(repo: Path) -> None:
    # stacks/telemetry is no stack, but any stack's services may mount it.
    before = git(repo, "rev-parse", "HEAD")
    (repo / "stacks" / "telemetry").mkdir()
    (repo / "stacks" / "telemetry" / "telemetry.py").write_text("# changed\n", encoding="utf-8")
    _commit_all(repo)

    assert deploy.changed_stacks(repo, before) is None


def test_a_change_to_another_script_deploys_no_stack(repo: Path) -> None:
    before = git(repo, "rev-parse", "HEAD")
    (repo / "ci_supervisor.py").write_text("# changed\n", encoding="utf-8")
    _commit_all(repo)

    assert deploy.changed_stacks(repo, before) == ()


@pytest.mark.parametrize("since", ["", "0123456789abcdef0123456789abcdef01234567"])
def test_a_missing_or_unknown_last_deploy_selects_all_stacks(repo: Path, since: str) -> None:
    assert deploy.changed_stacks(repo, since) is None


def test_deploys_changed_stacks_and_records_the_commit(
    repo: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path_factory.mktemp("state") / "last-deployed"
    state.write_text(git(repo, "rev-parse", "HEAD") + "\n", encoding="utf-8")
    (repo / "stacks" / "gateway" / "compose.yaml").write_text(
        "services: {app: {image: new}}\n", encoding="utf-8"
    )
    head = _commit_all(repo)
    runner, calls = _recording_runner()

    assert deploy.run(repo, (), last_deployed=state, runner=runner) == 0

    composed = {command[command.index("-f") + 1] for command, _ in calls if "-f" in command}
    assert composed == {str(repo / "stacks" / "gateway" / "compose.yaml")}
    assert state.read_text(encoding="utf-8").strip() == head


def test_a_secret_rendered_since_the_last_deploy_deploys_its_stack(
    repo: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    stack = repo / "stacks" / "observability"
    (stack / "compose.yaml").write_text(
        "services: {}\nsecrets: {token: {file: ../../secrets/token}}\n", encoding="utf-8"
    )
    state = tmp_path_factory.mktemp("state") / "last-deployed"
    state.write_text(_commit_all(repo) + "\n", encoding="utf-8")
    (repo / "secrets").mkdir()
    (repo / "secrets" / "token").write_text("new", encoding="utf-8")
    os.utime(state, ns=(0, 0))
    runner, calls = _recording_runner()

    assert deploy.run(repo, (), last_deployed=state, runner=runner) == 0

    composed = {command[command.index("-f") + 1] for command, _ in calls if "-f" in command}
    assert composed == {str(stack / "compose.yaml")}


def test_a_failed_deploy_keeps_the_old_marker_so_the_next_retries_in_full(
    repo: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    state = tmp_path_factory.mktemp("state") / "last-deployed"
    before = git(repo, "rev-parse", "HEAD")
    state.write_text(before + "\n", encoding="utf-8")
    (repo / "stacks" / "deploy" / "deploy.py").write_text("# changed\n", encoding="utf-8")
    _commit_all(repo)
    runner, _ = _recording_runner(fail_when="up")

    assert deploy.run(repo, (), last_deployed=state, runner=runner) == 1

    assert state.read_text(encoding="utf-8").strip() == before


def test_each_deploy_is_one_trace_with_its_commit_and_a_span_per_stack(
    repo: Path, tmp_path_factory: pytest.TempPathFactory, sent_traces: list[dict[str, Any]]
) -> None:
    state = tmp_path_factory.mktemp("state") / "last-deployed"
    (repo / "stacks" / "deploy" / "deploy.py").write_text("# changed\n", encoding="utf-8")
    head = _commit_all(repo)
    runner, _ = _recording_runner(fail_when="up")

    assert deploy.run(repo, (), last_deployed=state, runner=runner) == 1
    # The 15-minute check finding nothing new sends no trace, so marks no deploy on the pages.
    state.write_text(head + "\n", encoding="utf-8")
    assert deploy.run(repo, (), last_deployed=state, runner=_recording_runner()[0]) == 0

    [body] = sent_traces
    spans = {
        s["name"]: s for r in body["resourceSpans"] for c in r["scopeSpans"] for s in c["spans"]
    }
    assert set(spans) == {"deploy", "gateway", "observability"}
    root = spans["deploy"]
    assert {"key": "commit", "value": {"stringValue": head}} in root["attributes"]
    assert spans["gateway"]["parentSpanId"] == root["spanId"]
    assert spans["gateway"]["status"]["code"] == root["status"]["code"] == 2


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


def _describing_runner() -> tuple[deploy.CommandRunner, Calls]:
    runner, calls = _recording_runner()

    def describe(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if "--format" in command:
            stack = Path(command[command.index("-f") + 1]).parent.name
            return subprocess.CompletedProcess(
                command, 0, json.dumps(_COMPOSE_CONFIG[stack]).encode()
            )
        return runner(command, **kwargs)

    return describe, calls


@pytest.fixture
def stacks(repo: Path) -> Path:
    for name in ("backup", "web"):
        _make_stack(repo, name)
    _commit_all(repo)
    return repo


def _change(repo_root: Path, *names: str) -> str:
    before = git(repo_root, "rev-parse", "HEAD")
    for name in names:
        (repo_root / "stacks" / name / "extra.txt").write_text("x\n", encoding="utf-8")
    _commit_all(repo_root)
    return before


def _up_order(calls: Calls) -> list[str]:
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
def test_with_no_known_before_commit_each_stack_waits_for_what_it_uses(
    stacks: Path, since: str
) -> None:
    # A fresh host: every stack is new, and still needs the networks and volumes of others.
    runner, calls = _describing_runner()

    assert deploy.run(stacks, (), first_changed_since=since, runner=runner) == 0

    assert _up_order(calls) == ["gateway", "observability", "backup", "web"]
