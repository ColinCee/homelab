"""Alloy (stacks/observability/config.alloy) keeps every metric and the container labels read, and
loads (its validate command)."""

import re
import subprocess
from pathlib import Path

import yaml

from tests.conftest import tool

ROOT = Path(__file__).parent.parent
STACK = ROOT / "stacks/observability"
CONFIG = (STACK / "config.alloy").read_text(encoding="utf-8")
READERS = (*sorted((STACK / "grafana/dashboards").glob("*.json")), STACK / "public_stats.py")


def queries() -> list[str]:
    """Every alert and dashboard query, and the scripts that read metrics."""
    rules = yaml.safe_load((STACK / "prometheus/rules.yml").read_text(encoding="utf-8"))
    texts = [str(rule["expr"]) for group in rules["groups"] for rule in group["rules"]]
    return texts + [reader.read_text(encoding="utf-8") for reader in READERS]


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


def test_the_config_loads() -> None:
    command = [tool("alloy"), "validate", str(STACK / "config.alloy")]
    ran = subprocess.run(command, capture_output=True, text=True, timeout=300)
    assert ran.returncode == 0, ran.stdout + ran.stderr
