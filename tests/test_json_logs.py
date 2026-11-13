"""Every container logs JSON (docs/architecture.md, "Monitoring and alerts"): each program that can
is set to, our Python services through the JsonLines block each one copies, and Alloy wraps the
rest."""

import ast
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from stacks.observability.incidents.relay import JsonLines

ROOT = Path(__file__).parent.parent
STACKS = ROOT / "stacks"

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


def services() -> list[tuple[Path, str, dict[str, Any]]]:
    found = []
    for compose in sorted(STACKS.glob("*/compose.yaml")):
        stack = yaml.safe_load(compose.read_text(encoding="utf-8"))
        found += [(compose.parent, name, service) for name, service in stack["services"].items()]
    return found


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


def python_services() -> list[Path]:
    """Each Python file a container runs: the path its command names, and the volume mounting it."""
    found: list[Path] = []
    for stack, name, service in services():
        parts = [*_listed(service.get("entrypoint")), *_listed(service.get("command"))]
        for script in re.findall(r"(/[\w./-]+\.py)\b", " ".join(parts)):
            mounts = [v.split(":") for v in service.get("volumes", [])]
            sources = [m[0] for m in mounts if len(m) > 1 and m[1] == script]
            assert sources, f"{name}: no volume mounts {script}"
            found.append((stack / sources[0]).resolve())
    return found


def _listed(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def test_python_services_log_through_the_json_formatter() -> None:
    scripts = python_services()
    assert scripts
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        calls = [n.func for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Call)]
        assert not any(isinstance(f, ast.Name) and f.id == "print" for f in calls), script
        if any(isinstance(f, ast.Attribute) and f.attr in LOG_CALLS for f in calls):
            assert "handler.setFormatter(JsonLines())" in text, script
            assert any(isinstance(f, ast.Name) and f.id == "log_json" for f in calls), script


def block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index("# One JSON object per line")
    end = text.index("\n\n\n", text.index("def log_json"))
    return text[start:end]


def test_every_service_has_the_same_copy() -> None:
    copies = [p for p in python_services() if "JsonLines" in p.read_text(encoding="utf-8")]
    for path in copies[1:]:
        assert block(path) == block(copies[0]), path


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
