"""Grafana's alert rules (stacks/observability/provisioning/alerting/rules.yaml) stay paused:
OpenObserve's rules alert instead, and a live Grafana rule would post twice."""

from pathlib import Path

import yaml

RULES = Path(__file__).parent.parent / "stacks/observability/provisioning/alerting/rules.yaml"


def test_every_grafana_rule_is_paused() -> None:
    groups = yaml.safe_load(RULES.read_text(encoding="utf-8"))["groups"]
    rules = [r for g in groups for r in g["rules"]]
    assert rules and all(r.get("isPaused") is True for r in rules)
