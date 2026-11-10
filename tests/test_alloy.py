"""Alloy (stacks/observability/config.alloy) sends every metric something reads."""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent
STACK = ROOT / "stacks/observability"
READERS = (
    "scripts/server-health.sh",
    "stacks/observability/public_stats.py",
    ".github/workflows/heartbeat.yaml",
)
# A metric name as queries write it: before a selector, a range, a closing bracket or a quote.
NAME = r"\b(?:up|(?:node|container|probe|alertmanager|homelab)_\w+)\b(?=\s*[{\[)\"]|\s*$)"
# Labels read beside the metrics, which look like metric names.
LABELS = {"container_label_com_docker_compose_project", "container_name"}


def kept() -> re.Pattern[str]:
    config = (STACK / "config.alloy").read_text(encoding="utf-8")
    block = config[config.index('prometheus.relabel "read"') :]
    regex = re.search(r'regex\s*=\s*"([^"]+)"', block)[1]
    # As Prometheus relabelling matches: the whole name. Python takes the flag only at the start.
    assert regex.startswith("(?i)"), "OpenObserve lower-cases names, so the list does too"
    return re.compile(f"^(?:{regex.removeprefix('(?i)')})$", re.IGNORECASE)


def read() -> set[str]:
    """Each metric an alert, a dashboard, server-health, public stats or the heartbeat reads."""
    rules = tomllib.loads((STACK / "openobserve/alerts.toml").read_text(encoding="utf-8"))["rules"]
    texts = [q for rule in rules.values() for q in (rule["promql"], rule.get("no_data", ""))]
    for dashboard in (STACK / "openobserve/dashboards").glob("*.json"):
        texts += [
            q["query"]
            for tab in json.loads(dashboard.read_text(encoding="utf-8"))["tabs"]
            for panel in tab["panels"]
            for q in panel["queries"]
        ]
    texts += [(ROOT / reader).read_text(encoding="utf-8") for reader in READERS]
    return {name for text in texts for name in re.findall(NAME, text)} - LABELS


def test_every_metric_read_is_sent() -> None:
    names = read()
    assert {"up", "node_cpu_seconds_total", "container_last_seen", "homelab_probe_success"} <= names
    pattern = kept()
    assert not [n for n in sorted(names) if not pattern.match(n)]


def test_what_nothing_reads_is_not_sent() -> None:
    pattern = kept()
    for name in ("node_disk_io_time_seconds_total", "container_fs_writes_total", "go_goroutines"):
        assert not pattern.match(name), name
