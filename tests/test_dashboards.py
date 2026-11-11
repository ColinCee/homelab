"""Grafana's Home (stacks/observability/grafana/home.json) keeps the layout, colours and queries in
.github/skills/building-dashboards/SKILL.md."""

import json
import re
from pathlib import Path

import yaml

STACK = Path(__file__).parent.parent / "stacks/observability"
HOME = json.loads((STACK / "grafana/home.json").read_text(encoding="utf-8"))
SOURCES = {"prometheus", "alertmanager", "logs", "traces"}

# These dataviz steps pass contrast checks on Grafana's light and dark themes.
GROUPS = {"Monitoring": "#2a78d6", "System": "#199e70", "Apps": "#d95926"}
NOW, WEEK, NEUTRAL = "#d03b3b", "#fab219", "#898781"

# Alloy sends these in mixed case; a lower-case copy finds nothing.
MIXED_CASE = {
    name.lower(): name
    for name in (
        "node_memory_MemAvailable_bytes",
        "node_memory_MemTotal_bytes",
        "node_memory_SwapTotal_bytes",
        "node_memory_SwapFree_bytes",
        "node_netstat_TcpExt_TCPSynRetrans",
        "node_netstat_Tcp_OutRsts",
        "node_netstat_TcpExt_ListenDrops",
    )
}


def panels(home: dict) -> list[dict]:
    """Every panel, those inside collapsed rows included."""
    found = []
    for panel in home["panels"]:
        found.append(panel)
        found += panel.get("panels", [])
    return found


def charts(home: dict) -> list[dict]:
    return [p for p in panels(home) if p["type"] != "row"]


def uid(panel: dict) -> str:
    return (panel.get("datasource") or {}).get("uid", "")


def series_colours(panel: dict) -> dict[str, str]:
    return {
        o["matcher"]["options"]: prop["value"]["fixedColor"]
        for o in panel.get("fieldConfig", {}).get("overrides", [])
        if o["matcher"]["id"] == "byName"
        for prop in o["properties"]
        if prop["id"] == "color"
    }


def prometheus_queries(home: dict) -> list[str]:
    return [
        t["expr"]
        for p in charts(home)
        for t in p["targets"]
        if t.get("datasource", p.get("datasource", {})).get("uid") == "prometheus"
    ]


def containers() -> dict[str, str]:
    """Each container compose starts, in its group's colour."""
    names = {}
    for compose in STACK.parent.glob("*/compose.yaml"):
        services = yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]
        for service, spec in services.items():
            name = spec.get("container_name", f"{compose.parent.name}-{service}-1")
            names[name] = GROUPS["Monitoring" if compose.parent.name == "observability" else "Apps"]
    return names


def levels() -> dict[str, str]:
    """Each alert rule, in its level's colour."""
    groups = yaml.safe_load((STACK / "prometheus/rules.yml").read_text(encoding="utf-8"))["groups"]
    found = {r["alert"]: r["labels"]["level"] for g in groups for r in g["rules"]}
    # Always firing, so the dashboards leave it out.
    found.pop("watchdog")
    return {name: NOW if level == "now" else WEEK for name, level in found.items()}


def test_home_is_read_only_and_its_log_panels_read_victorialogs() -> None:
    # A dashboard exported from Grafana's editor comes back editable and with an id.
    assert HOME["editable"] is False and "id" not in HOME
    logs = [p for p in charts(HOME) if uid(p) == "logs"]
    assert logs and all(p["datasource"]["type"] == "victoriametrics-logs-datasource" for p in logs)


def test_every_panel_reads_a_provisioned_source() -> None:
    ids = [p["id"] for p in panels(HOME)]
    assert len(ids) == len(set(ids))
    for panel in charts(HOME):
        assert uid(panel) in SOURCES, panel["title"]
        assert panel["targets"], panel["title"]
        refs = [t["refId"] for t in panel["targets"]]
        assert len(refs) == len(set(refs)), panel["title"]
        for target in panel["targets"]:
            assert target["datasource"]["uid"] in SOURCES, panel["title"]


def test_no_two_panels_overlap_and_all_fit_the_width() -> None:

    def place(group: list[dict]) -> None:
        cells: set[tuple[int, int]] = set()
        for panel in group:
            box = panel["gridPos"]
            assert box["x"] + box["w"] <= 24, panel["title"]
            mine = {
                (x, y)
                for x in range(box["x"], box["x"] + box["w"])
                for y in range(box["y"], box["y"] + box["h"])
            }
            assert not cells & mine, panel["title"]
            cells |= mine

    # What shows with every row closed, then each closed row's panels as it opens.
    place(HOME["panels"])
    for row in HOME["panels"]:
        if row["type"] == "row" and row["collapsed"]:
            assert all(p["gridPos"]["y"] > row["gridPos"]["y"] for p in row["panels"])
            place(row["panels"])
        elif row["type"] == "row":
            assert row["panels"] == []


def test_groups_containers_and_rules_wear_one_colour_on_every_panel() -> None:
    every = charts(HOME)
    by_container = [p for p in every if set(series_colours(p)) & set(containers())]
    assert by_container
    for panel in by_container:
        assert series_colours(panel) == containers(), panel["title"]
    for panel in every:
        for group in set(series_colours(panel)) & set(GROUPS):
            assert series_colours(panel)[group] == GROUPS[group], panel["title"]
    rules = next(p for p in every if p["title"] == "Rules firing, by hour")
    assert series_colours(rules) == levels()


def test_tiles_are_red_or_amber_and_unlisted_series_gray() -> None:
    for panel in charts(HOME):
        defaults = panel.get("fieldConfig", {}).get("defaults", {})
        if panel["type"] == "stat":
            steps = defaults["thresholds"]["steps"]
            assert {s["color"] for s in steps} <= {"text", NOW, WEEK}, panel["title"]
        elif "color" in defaults:
            # A series no list names, like a removed container, is drawn gray.
            assert defaults["color"] == {"mode": "fixed", "fixedColor": NEUTRAL}, panel["title"]


def test_prometheus_queries_name_metrics_as_alloy_sends_them() -> None:
    for query in prometheus_queries(HOME):
        bare = re.sub(r'"[^"]*"', '""', query)
        for name in re.findall(r"\b[a-zA-Z_:][a-zA-Z0-9_:]*\b", bare):
            assert MIXED_CASE.get(name.lower(), name) == name, query
        for left, right in ("()", "[]", "{}"):
            assert bare.count(left) == bare.count(right), query
    # Grafana's legends are `{{label}}`; a single-braced `{label}` shows as text.
    for panel in charts(HOME):
        for target in panel["targets"]:
            legend = target.get("legendFormat", "")
            assert not re.search(r"(?<!\{)\{\w+\}(?!\})", legend), panel["title"]


def test_log_patterns_hold_no_control_characters() -> None:
    # A "\b" written through a non-raw string becomes a backspace and the pattern matches nothing.
    for panel in charts(HOME):
        for target in panel["targets"]:
            assert target["expr"].isprintable(), panel["title"]
