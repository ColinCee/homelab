"""Every container logs JSON (docs/architecture.md, "Monitoring and alerts"): each program that can
is set to, our Python services through the one telemetry module (stacks/telemetry/telemetry.py),
and Alloy wraps the rest."""

import ast
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from telemetry import JsonLines

from conftest import STACKS, dockerfile, programs, services, tracked, units

# Where each image is told to log JSON: an argument in its command, or NAME=json in its environment;
# None when it logs JSON with no setting. A new image fails until it is added here or to TEXT_ONLY.
JSON_SETTINGS: dict[str, tuple[str, str] | None] = {
    "prom/prometheus": ("command", "--log.format=json"),
    "prom/alertmanager": ("command", "--log.format=json"),
    "prom/blackbox-exporter": ("command", "--log.format=json"),
    "victoriametrics/victoria-logs": ("command", "-loggerFormat=json"),
    "victoriametrics/victoria-traces": ("command", "-loggerFormat=json"),
    "homelab/grafana": ("environment", "GF_LOG_CONSOLE_FORMAT"),
    "otel/ebpf-instrument": ("environment", "OTEL_EBPF_LOG_FORMAT"),
    "cloudflare/cloudflared": ("environment", "TUNNEL_LOG_OUTPUT"),
    # Its `logging` block: test_alloy_logs_json.
    "grafana/alloy": None,
    # Caddy's own logs are JSON unless a Caddyfile sets another format: test_caddy_logs_json.
    "caddy": None,
    # Our Python services: test_python_services_log_through_the_json_formatter.
    "python": None,
    "homelab/backup": None,
    "homelab/public-stats": None,
}
# No JSON setting: Alloy wraps their lines (config.alloy, loki.process "docker").
TEXT_ONLY = {
    "homeassistant/home-assistant",
    "coredns/coredns",
    "linuxserver/socket-proxy",
    "ghcr.io/colincee/flight-tracker-at-home/backend",
}
LOG_CALLS = {"debug", "info", "warning", "error", "exception", "critical"}


def test_every_image_logs_json_or_is_wrapped() -> None:
    for _, name, service in services():
        image = re.split(r"[:@]", service["image"], maxsplit=1)[0]
        if image in TEXT_ONLY:
            continue
        assert image in JSON_SETTINGS, f"{name}: say how {image} logs JSON, or add it to TEXT_ONLY"
        setting = JSON_SETTINGS[image]
        if setting is None:
            continue
        where, value = setting
        if where == "command":
            assert value in service.get("command", []), f"{name}: no {value}"
        else:
            assert service.get("environment", {}).get(value) == "json", f"{name}: {value} not json"


def test_caddy_logs_json() -> None:
    for caddyfile in STACKS.glob("**/Caddyfile"):
        text = caddyfile.read_text(encoding="utf-8")
        assert not re.search(r"^\s*format\s+(?!json\b)", text, re.MULTILINE), caddyfile


def test_alloy_logs_json() -> None:
    config = (STACKS / "observability/config.alloy").read_text(encoding="utf-8")
    assert re.search(r'^logging \{\n  format = "json"\n\}', config, re.MULTILINE)


def python_services() -> list[tuple[str, dict[str, Any], Path]]:
    """Each Python file a container runs: the path its command names, and the volume mounting it."""
    found: list[tuple[str, dict[str, Any], Path]] = []
    for stack, name, service in services():
        parts = [*_listed(service.get("entrypoint")), *_listed(service.get("command"))]
        for script in re.findall(r"(/[\w./-]+\.py)\b", " ".join(parts)):
            mounts = [v.split(":") for v in service.get("volumes", [])]
            sources = [m[0] for m in mounts if len(m) > 1 and m[1] == script]
            assert sources, f"{name}: no volume mounts {script}"
            found.append((name, service, (stack / sources[0]).resolve()))
    return found


def python_version(stack: Path, service: dict[str, Any]) -> str:
    """The image's Python as major.minor: from its tag, or its Dockerfile's FROM."""
    image = str(service["image"])
    if "build" in service:
        path = dockerfile(stack, service)
        base = re.search(r"^FROM (\S+)", path.read_text("utf-8"), re.MULTILINE)
        assert base, path
        image = base.group(1)
    version = re.match(r"python:(\d+\.\d+)", image)
    assert version, image
    return str(version.group(1))


def _listed(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def test_python_services_log_through_the_telemetry_module() -> None:
    found = python_services()
    assert found
    by_name = {name: (stack, service) for stack, name, service in services()}
    for name, service, script in found:
        text = script.read_text(encoding="utf-8")
        calls = [n.func for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Call)]
        assert not any(isinstance(f, ast.Name) and f.id == "print" for f in calls), script
        logs = any(isinstance(f, ast.Attribute) and f.attr in LOG_CALLS for f in calls)
        if logs:
            started = [f for f in calls if isinstance(f, ast.Attribute) and f.attr == "start"]
            assert any(
                isinstance(f.value, ast.Name) and f.value.id == "telemetry" for f in started
            ), script
        elif "\nimport telemetry\n" not in text:
            continue
        # Its container has the module where `python -I` finds it: its Python's site-packages.
        stack, _ = by_name[name]
        site = f"/usr/local/lib/python{python_version(stack, service)}/site-packages/telemetry.py"
        mounts = [v.split(":")[:2] for v in service.get("volumes", [])]
        assert ["../telemetry/telemetry.py", site] in mounts, f"{name}: mount {site}"


def test_traced_services_reach_alloy() -> None:
    # A trace that cannot reach Alloy is only a warning, and its lines' trace_id opens nothing.
    traced = [
        (name, service)
        for name, service, script in python_services()
        if "telemetry.run(" in script.read_text(encoding="utf-8")
    ]
    assert {name for name, _ in traced} >= {"backup", "public-stats"}
    for name, service in traced:
        assert service["environment"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://alloy:4318", name
        assert {"otlp", "observability-otlp"} & set(service["networks"]), name


def test_the_formatter_lives_in_the_telemetry_module_alone() -> None:
    for path in programs():
        if path != STACKS / "telemetry/telemetry.py":
            assert "logging.Formatter" not in path.read_text(encoding="utf-8"), path


def line(record: logging.LogRecord) -> str:
    out = JsonLines().format(record)
    assert "\n" not in out
    return out


def test_a_line_carries_its_fields_and_no_record_internals() -> None:
    logger = logging.getLogger("relay-test")
    record = logger.makeRecord(
        "relay-test", logging.ERROR, __file__, 1, "cannot post", (), None,
        extra={"post": "123", "error": "timed out"},
    )  # fmt: skip
    got = json.loads(line(record))
    assert got.pop("time")
    assert got == {
        "level": "ERROR",
        "logger": "relay-test",
        "msg": "cannot post",
        "post": "123",
        "error": "timed out",
    }


def test_an_exception_is_kept_by_its_semantic_convention_names() -> None:
    try:
        raise ValueError("bad")
    except ValueError:
        record = logging.getLogger("x").makeRecord(
            "x", logging.ERROR, __file__, 1, "crashed", (), sys.exc_info()
        )
    got = json.loads(line(record))
    assert got["exception.type"] == "ValueError"
    assert got["exception.message"] == "bad"
    assert "raise ValueError" in str(got["exception.stacktrace"])


def test_python_services_stop_when_asked() -> None:
    # Python as PID 1 ignores SIGTERM, so a deploy waits 10 s and kills it (docs/architecture.md,
    # "Deploys").
    for name, service, _ in python_services():
        assert service.get("init") is True, f"{name}: init: true"


ROOT = STACKS.parent
# Host programs that print rather than log, and why.
OWN_OUTPUT = {
    # The installer side imports nothing from the checkout (tests/test_install_host.py).
    "ci-runner/update.py",
    # Installed under net/, apart from the module, and run before anything else is set up.
    "ci-runner/net/setup_net.py",
}


def host_scripts() -> set[str]:
    """Each Python file a systemd unit runs on the host, directly or through its .sh wrapper."""
    found: set[str] = set()
    for unit in units():
        for line in re.findall(r"^ExecStart\S*=(.*)$", unit.read_text(encoding="utf-8"), re.M):
            if "docker exec" in line:
                continue  # a container's, which test_python_services_log_... checks
            for target in re.findall(r"(\S+\.(?:py|sh))\b", line):
                name = target.rsplit("/", 1)[-1]
                sources = [p for p in tracked() if p.name == name]
                assert len(sources) == 1, f"{unit.name}: {name} is {sources}"
                source = sources[0]
                if name.endswith(".sh"):
                    text = source.read_text(encoding="utf-8")
                    wrapped = re.search(r"^exec .*?(\w+\.py)", text, re.M)
                    assert wrapped, source
                    source = source.with_name(wrapped.group(1))
                found.add(source.relative_to(ROOT).as_posix())
    return found


def starts_telemetry_when_run(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test):
            called = {ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)}
            return "telemetry.start" in called
    return False


def test_host_scripts_log_through_the_telemetry_module() -> None:
    scripts = host_scripts()
    # The units' scripts, and deploy.py through deploy-main's deploy.sh.
    assert {"ci-runner/supervisor/ci_supervisor.py", "stacks/deploy/deploy_main.py"} <= scripts
    for name in sorted(scripts | {"stacks/deploy/deploy.py"}):
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        calls = [n.func for n in ast.walk(tree) if isinstance(n, ast.Call)]
        printed = any(isinstance(f, ast.Name) and f.id == "print" for f in calls)
        if name in OWN_OUTPUT:
            assert printed, f"{name} prints no more: drop it from OWN_OUTPUT"
            continue
        assert not printed, f"{name}: log through telemetry, not print"
        assert starts_telemetry_when_run(tree), f"{name}: call telemetry.start() under __main__"
