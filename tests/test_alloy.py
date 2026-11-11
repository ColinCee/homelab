"""Alloy (stacks/observability/config.alloy) keeps every metric and the container labels read, and
loads with each pick of log and trace store (its validate command, so that test needs Docker)."""

import functools
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
STACK = ROOT / "stacks/observability"
CONFIG = (STACK / "config.alloy").read_text(encoding="utf-8")
READERS = (
    "scripts/server-health.sh",
    "stacks/observability/public_stats.py",
    ".github/workflows/heartbeat.yaml",
)


def queries() -> list[str]:
    """Every alert and Home dashboard query, and the scripts that read metrics."""
    rules = yaml.safe_load((STACK / "prometheus/rules.yml").read_text(encoding="utf-8"))
    texts = [rule["expr"] for group in rules["groups"] for rule in group["rules"]]
    for dashboard in STACK.glob("logs/*/home.json"):
        texts.append(dashboard.read_text(encoding="utf-8"))
    return texts + [(ROOT / reader).read_text(encoding="utf-8") for reader in READERS]


def test_no_metric_is_dropped_by_name() -> None:
    # The store keeps what a later fix may need, so no rule filters on the metric name.
    assert not re.search(r'source_labels\s*=\s*\[[^\]]*"__name__"', CONFIG)
    # Nor do the host and container exporters narrow their default sets.
    assert "set_collectors" not in CONFIG
    docker = CONFIG[CONFIG.index('prometheus.exporter.cadvisor "docker"') :]
    assert "enabled_metrics" not in docker[: docker.index("\n}")]


def test_every_container_label_read_is_sent() -> None:
    block = CONFIG[CONFIG.index("allowlisted_container_labels") :]
    allowed = re.findall(r'"([^"]+)"', block[: block.index("]")])
    sent = {"container_label_" + re.sub(r"[.-]", "_", label) for label in allowed}
    read = {name for text in queries() for name in re.findall(r"\bcontainer_label_\w+", text)}
    assert "container_label_com_docker_compose_project" in read
    assert read <= sent


@functools.cache
def docker() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0


@pytest.mark.skipif("not docker()", reason="needs Docker")
@pytest.mark.parametrize("logs", ["loki", "victorialogs"])
@pytest.mark.parametrize("traces", ["tempo", "victoriatraces"])
def test_the_config_loads_with_each_store(logs: str, traces: str) -> None:
    image = yaml.safe_load((STACK / "compose.yaml").read_text(encoding="utf-8"))["services"]
    command = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{STACK / 'config.alloy'}:/etc/alloy/config.alloy:ro",
    ]
    command += ["-v", f"{STACK / 'logs' / logs / 'alloy.alloy'}:/etc/alloy/logs.alloy:ro"]
    command += ["-v", f"{STACK / 'traces' / traces / 'alloy.alloy'}:/etc/alloy/traces.alloy:ro"]
    command += [image["alloy"]["image"], "validate", "/etc/alloy"]
    ran = subprocess.run(command, capture_output=True, text=True, timeout=300)
    assert ran.returncode == 0, ran.stdout + ran.stderr


def test_a_line_keeps_its_trace_so_grafana_can_link_it() -> None:
    # JSON Docker lines carry it under OpenTelemetry's names; programs' OTLP logs carry their own.
    docker = CONFIG[CONFIG.index('loki.process "docker"') :]
    docker = docker[: docker.index("\n}\n")]
    assert re.search(r'trace_id\s*=\s*"trace_id \|\| traceId', docker)
    metadata = docker[docker.index("stage.structured_metadata") :]
    assert re.search(r'trace_id\s*=\s*"trace_id"', metadata)
    receiver = CONFIG[CONFIG.index('otelcol.receiver.otlp "programs"') :]
    receiver = receiver[: receiver.index("\n}\n")]
    assert re.search(r"logs\s*=\s*\[otelcol\.exporter\.loki\.programs\.input\]", receiver)
    assert re.search(r"traces\s*=\s*\[otelcol\.processor\.batch\.traces\.input\]", receiver)
    # They pass the secret filter in Loki's form, so their trace ID comes back out of the JSON line.
    programs = CONFIG[CONFIG.index('loki.process "programs"') :]
    programs = programs[: programs.index("\n}\n")]
    assert re.search(r'trace_id\s*=\s*"traceid"', programs)
    assert re.search(
        r'trace_id\s*=\s*"trace_id"', programs[programs.index("structured_metadata") :]
    )
