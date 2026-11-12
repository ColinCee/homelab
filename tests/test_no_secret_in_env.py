"""No secret goes in a container's environment, command or labels: anything allowed to inspect
containers reads them, Alloy's Docker API proxy included (stacks/observability/compose.yaml,
docker-socket-proxy). A secret is a file under /run/secrets (scripts/render_secrets.py)."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import render_secrets

ROOT = Path(__file__).parent.parent
STACKS = ROOT / "stacks"


class ComposeLoader(yaml.SafeLoader):
    """Reads Compose's own tags (`!reset`, `!override`) as plain values."""


def _plain(loader: yaml.SafeLoader, _tag: str, node: yaml.Node) -> object:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


ComposeLoader.add_multi_constructor("!", _plain)


def load(path: Path) -> object:
    return yaml.load(path.read_text(encoding="utf-8"), ComposeLoader)


def load_compose(path: Path) -> dict[str, Any]:
    spec = yaml.load(path.read_text(encoding="utf-8"), ComposeLoader)
    assert isinstance(spec, dict), path
    return spec


COMPOSE_FILES = sorted(
    path
    for path in [*STACKS.rglob("*.yaml"), *STACKS.rglob("*.yml")]
    if isinstance(spec := load(path), dict) and "services" in spec
)
SECRET_NAME = re.compile(r"PASSWORD|SECRET|TOKEN|KEY|WEBHOOK|LOGIN|CREDENTIAL", re.IGNORECASE)
# A path to a secret file, which programs name *_FILE (or Grafana's *__FILE).
FILE_OPTION = re.compile(r"_FILE$", re.IGNORECASE)
# `$$` is Compose's escape for a literal `$`, which interpolates nothing.
INTERPOLATED = re.compile(r"(?<!\$)(?:\$\$)*\$\{?([A-Za-z_][A-Za-z0-9_]*)")


def secret_like(name: str) -> bool:
    return bool(SECRET_NAME.search(name)) and not FILE_OPTION.search(name)


def environment(service: dict[str, Any]) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
    return {name: str(value) for name, value in env.items()}


def problems(compose: dict[str, Any]) -> list[str]:
    found = []
    for name, service in compose.get("services", {}).items():
        if "env_file" in service:
            found.append(f"{name}: env_file puts a whole file in its environment")
        for variable in environment(service):
            if secret_like(variable):
                found.append(f"{name}: {variable} in its environment")
        # Its environment, command, entrypoint and labels: everything but its secrets list.
        shown = yaml.safe_dump({k: v for k, v in service.items() if k != "secrets"})
        for variable in INTERPOLATED.findall(shown):
            if secret_like(variable):
                found.append(f"{name}: interpolates {variable}")
    return found


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: str(p.relative_to(STACKS)))
def test_no_secret_in_a_containers_environment(path: Path) -> None:
    assert problems(load_compose(path)) == []


@pytest.mark.parametrize(
    "service",
    [
        {"environment": {"RESTIC_PASSWORD": "${RESTIC_PASSWORD:?}"}},
        {"environment": ["TUNNEL_TOKEN=x"]},
        {"environment": {"LOGIN": "stats:${STATS_PASSWORD:?}"}},
        {"command": ["run", "--token", "${CF_TUNNEL_TOKEN}"]},
        {"labels": {"x": "$GRAFANA_SECRET_KEY"}},
        {"environment": {"ALERTS": "${DISCORD_PRIVATE_WEBHOOK_URL}"}},
        {"env_file": [".env"]},
    ],
)
def test_each_way_a_secret_reaches_the_environment_is_caught(service: dict[str, Any]) -> None:
    assert problems({"services": {"s": service}})


def test_each_secret_file_a_stack_mounts_is_one_the_deploy_writes_and_each_is_used() -> None:
    written = {name.lower() for name in render_secrets.secret_names(ROOT)}
    assert "restic_password" in written
    mounted = set()
    for compose in STACKS.glob("*/compose.yaml"):
        spec = load_compose(compose)
        for secret in (spec.get("secrets") or {}).values():
            folder, _, name = secret["file"].rpartition("/")
            assert folder == "../../secrets", compose
            mounted.add(name)
    assert mounted == written


def test_no_secret_reaches_a_stacks_settings() -> None:
    """Compose interpolates only what a stack's settings.env holds."""
    names = render_secrets.secret_names(ROOT)
    for settings in STACKS.glob("*/settings.env"):
        text = settings.read_text(encoding="utf-8")
        assert not [name for name in names if name in text], settings


def test_a_secret_file_and_its_path_pass() -> None:
    service = {
        "environment": {"RESTIC_PASSWORD_FILE": "/run/secrets/restic_password", "TZ": "Etc/UTC"},
        "command": ["sh", "-c", "echo $$TOKEN"],
        "secrets": ["restic_password"],
    }
    assert problems({"services": {"s": service}}) == []
