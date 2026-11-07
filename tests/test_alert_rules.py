"""Grafana's alert rules carry what Auto Kaizen's #alerts posts show: a plain name, the
measure, when a measured one triggers and, for number alerts, the chart's query (the rule's own)."""

import re
from pathlib import Path

import yaml

ALERTING = Path(__file__).parent.parent / "stacks/observability/provisioning/alerting"
GROUPS = yaml.safe_load((ALERTING / "rules.yaml").read_text(encoding="utf-8"))["groups"]
RULES = [r for g in GROUPS for r in g["rules"]]


def test_every_rule_names_its_measure_and_a_measured_one_its_trigger() -> None:
    for rule in RULES:
        notes = rule["annotations"]
        assert notes.get("measure"), rule["uid"]
        # A rule that names a thing (a new device) has no threshold, so no Triggers at box.
        measured = "$values" in notes.get("value", "")
        assert bool(notes.get("trigger")) == measured, rule["uid"]
        assert len(rule["title"].split()) <= 8, f"{rule['uid']}: say the symptom in a few words"


def test_a_chart_queries_what_its_rule_alerts_on() -> None:
    for rule in RULES:
        if "chart" in rule["annotations"]:
            expr = next(d for d in rule["data"] if d["refId"] == "A")["model"]["expr"]
            # The rule's own query, at most scaled to the box's unit ("100 * (...)" for percent).
            assert " ".join(expr.split()) in " ".join(rule["annotations"]["chart"].split()), rule[
                "uid"
            ]
            float(rule["annotations"]["chart_line"])


def test_only_the_control_rooms_outage_goes_straight_to_discord() -> None:
    policy = yaml.safe_load((ALERTING / "policies.yaml").read_text(encoding="utf-8"))["policies"][0]
    [[label, op, name]] = policy["routes"][0]["object_matchers"]
    assert (label, op) == ("alertname", "=")
    assert name == "Kaizen Control room unreachable"
    assert name in {r["title"] for r in RULES}


def test_annotations_name_only_their_rules_queries() -> None:
    for rule in RULES:
        refs = {d["refId"] for d in rule["data"]}
        for text in rule["annotations"].values():
            for name in re.findall(r"\$values\.(\w+)", text):
                assert name in refs, f"{rule['uid']}: no query {name}"
