"""Alloy (stacks/observability/config.alloy) keeps every metric and the container labels read."""

import json
import re
import tomllib
from pathlib import Path

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
    rules = tomllib.loads((STACK / "openobserve/alerts.toml").read_text(encoding="utf-8"))["rules"]
    texts = [q for rule in rules.values() for q in (rule["promql"], rule.get("no_data", ""))]
    for dashboard in (STACK / "openobserve/dashboards").glob("*.json"):
        texts += [
            q["query"]
            for tab in json.loads(dashboard.read_text(encoding="utf-8"))["tabs"]
            for panel in tab["panels"]
            for q in panel["queries"]
        ]
    return texts + [(ROOT / reader).read_text(encoding="utf-8") for reader in READERS]


def test_no_metric_is_dropped_by_name() -> None:
    # The store keeps what a later fix may need, so no rule filters on the metric name.
    assert not re.search(r'source_labels\s*=\s*\[[^\]]*"__name__"', CONFIG)


def test_every_container_label_read_is_sent() -> None:
    block = CONFIG[CONFIG.index("allowlisted_container_labels") :]
    allowed = re.findall(r'"([^"]+)"', block[: block.index("]")])
    sent = {"container_label_" + re.sub(r"[.-]", "_", label) for label in allowed}
    read = {name for text in queries() for name in re.findall(r"\bcontainer_label_\w+", text)}
    assert "container_label_com_docker_compose_project" in read
    assert read <= sent
