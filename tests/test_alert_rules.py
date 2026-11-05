"""Grafana's alert rules carry what Auto Kaizen's #alerts posts show: a plain name, the
measure, when it triggers and, for number alerts, the chart's query (the rule's own)."""

import re
from pathlib import Path

import yaml

ALERTING = Path(__file__).parent.parent / "stacks/observability/provisioning/alerting"
GROUPS = yaml.safe_load((ALERTING / "rules.yaml").read_text(encoding="utf-8"))["groups"]
RULES = [r for g in GROUPS for r in g["rules"]]


def test_every_rule_names_its_measure_and_trigger() -> None:
    for rule in RULES:
        notes = rule["annotations"]
        assert notes.get("measure") and notes.get("trigger"), rule["uid"]
        assert len(rule["title"].split()) <= 8, f"{rule['uid']}: say the symptom in a few words"


def test_a_chart_queries_what_its_rule_alerts_on() -> None:
    for rule in RULES:
        if "chart" in rule["annotations"]:
            expr = next(d for d in rule["data"] if d["refId"] == "A")["model"]["expr"]
            assert rule["annotations"]["chart"].split() == expr.split(), rule["uid"]


def test_kaizens_own_failures_route_to_rules_that_exist() -> None:
    policy = yaml.safe_load((ALERTING / "policies.yaml").read_text(encoding="utf-8"))["policies"][0]
    [[_, _, pattern]] = policy["routes"][0]["object_matchers"]
    titles = {r["title"] for r in RULES}
    for name in pattern.split("|"):
        assert name in titles or name.startswith("Datasource"), name
    assert re.fullmatch(pattern, "Kaizen Control room unreachable")
