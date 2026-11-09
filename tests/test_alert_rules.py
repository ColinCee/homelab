"""Grafana's alert rules carry what Auto Kaizen's #alerts posts show: a plain name, the
measure, when a measured one triggers and, for number alerts, the chart's query (the rule's own)."""

import re
from pathlib import Path

import yaml

ALERTING = Path(__file__).parent.parent / "stacks/observability/provisioning/alerting"
PROVISIONED = yaml.safe_load((ALERTING / "rules.yaml").read_text(encoding="utf-8"))
GROUPS = PROVISIONED["groups"]
RULES = [r for g in GROUPS for r in g["rules"]]


def test_no_live_rule_is_also_deleted() -> None:
    deleted = {r["uid"] for r in PROVISIONED.get("deleteRules", [])}
    assert not deleted & {r["uid"] for r in RULES}


def test_every_rule_names_its_measure_and_a_measured_one_its_trigger() -> None:
    for rule in RULES:
        notes = rule["annotations"]
        assert notes.get("measure"), rule["uid"]
        # A rule that names a thing (an unknown container) has no threshold, so no Triggers at box.
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


def test_only_the_outage_skips_kaizen_and_colins_alerts_group_per_thing() -> None:
    policy = yaml.safe_load((ALERTING / "policies.yaml").read_text(encoding="utf-8"))["policies"][0]
    outage, colin = policy["routes"]
    # Colin's alerts still go to Kaizen, one group per container.
    assert colin["receiver"] == "Kaizen" and "name" in colin["group_by"]
    assert colin["object_matchers"] == [["route", "=", "colin"]]
    [[label, op, name]] = outage["object_matchers"]
    assert (label, op) == ("alertname", "=")
    assert name == "Kaizen Control room unreachable"
    assert name in {r["title"] for r in RULES}


def test_alerts_only_colin_can_act_on_say_what_to_ask_him() -> None:
    colins = [r for r in RULES if r["labels"].get("route") == "colin"]
    assert {r["title"] for r in colins} == {"Unknown container started", "Beelink SSD wearing out"}
    # The #alerts post's Needs you line: a few words.
    assert all(len(r["annotations"]["needs"].split()) <= 8 for r in colins)


def test_annotations_name_only_their_rules_queries() -> None:
    for rule in RULES:
        refs = {d["refId"] for d in rule["data"]}
        for text in rule["annotations"].values():
            for name in re.findall(r"\$values\.(\w+)", text):
                assert name in refs, f"{rule['uid']}: no query {name}"


def test_a_rules_closes_kind_is_one_kaizen_reads() -> None:
    # auto-kaizen src/kaizen/board/alert.py: anything else counts as a state rule.
    kinds = {r["annotations"]["closes"] for r in RULES if "closes" in r["annotations"]}
    assert kinds <= {"event", "good-news"}
