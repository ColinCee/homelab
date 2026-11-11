"""No secret goes in a container's environment, command or labels: anything allowed to inspect
containers reads them, Alloy's Docker API proxy included (stacks/observability/compose.yaml,
docker-socket-proxy). A secret is a file under /run/secrets (scripts/generate_env.py)."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
STACKS = ROOT / "stacks"


class ComposeLoader(yaml.SafeLoader):
    """Reads Compose's own tags (`!reset`, `!override`) as plain values."""


def _plain(loader: yaml.SafeLoader, _tag: str, node: yaml.Node) -> object:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)  # ty: ignore[invalid-argument-type]


ComposeLoader.add_multi_constructor("!", _plain)


def load(path: Path) -> object:
    return yaml.load(path.read_text(encoding="utf-8"), ComposeLoader)


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


def environment(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
    return {name: str(value) for name, value in env.items()}


def problems(compose: dict) -> list[str]:
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
    assert problems(load(path)) == []


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
def test_each_way_a_secret_reaches_the_environment_is_caught(service: dict) -> None:
    assert problems({"services": {"s": service}})


def test_no_deploy_secret_reaches_a_stacks_env_file() -> None:
    """Compose interpolates only what a stack's .env holds, so a GitHub secret kept out of every
    .env.example reaches a container only as a secret file, whatever it is named."""
    workflow = (ROOT / ".github/workflows/deploy.yaml").read_text(encoding="utf-8")
    deployed = set(re.findall(r"\$\{\{\s*secrets\.([A-Z0-9_]+)\s*\}\}", workflow))
    assert "RESTIC_PASSWORD" in deployed
    for example in STACKS.glob("*/.env.example"):
        used = set(re.findall(r"\$\{([A-Z0-9_]+)\}", example.read_text(encoding="utf-8")))
        assert not used & deployed, example


def test_a_secret_file_and_its_path_pass() -> None:
    service = {
        "environment": {"RESTIC_PASSWORD_FILE": "/run/secrets/restic_password", "TZ": "Etc/UTC"},
        "command": ["sh", "-c", "echo $$TOKEN"],
        "secrets": ["restic_password"],
    }
    assert problems({"services": {"s": service}}) == []
