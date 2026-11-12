"""Grafana's dashboards (stacks/observability/grafana/dashboards/) keep the layout, colours, links
and queries in .github/skills/building-dashboards/SKILL.md."""

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_alert_rules import promtool

STACK = Path(__file__).parent.parent / "stacks/observability"
PAGES: dict[str, dict[str, Any]] = {
    path.stem: json.loads(path.read_text(encoding="utf-8"))
    for path in sorted((STACK / "grafana/dashboards").glob("*.json"))
}
SOURCES = {"prometheus", "alertmanager", "logs", "traces"}

# These dataviz steps pass contrast checks on Grafana's light and dark themes.
GROUPS = {"Monitoring": "#2a78d6", "System": "#199e70", "Apps": "#d95926"}
NOW, WEEK, NEUTRAL, FINE = "#d03b3b", "#fab219", "#898781", "#2f8f46"
GRAY = {"mode": "fixed", "fixedColor": NEUTRAL}

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

every_page = pytest.mark.parametrize("page", PAGES.values(), ids=PAGES)


def panels(page: dict[str, Any]) -> list[dict[str, Any]]:
    """Every panel, those inside collapsed rows included."""
    found = []
    for panel in page["panels"]:
        found.append(panel)
        found += panel.get("panels", [])
    return found


def charts(page: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in panels(page) if p["type"] != "row"]


def uid(panel: dict[str, Any]) -> str:
    return str((panel.get("datasource") or {}).get("uid", ""))


def named_colours(panel: dict[str, Any]) -> dict[str, str]:
    return {
        o["matcher"]["options"]: prop["value"]["fixedColor"]
        for o in panel.get("fieldConfig", {}).get("overrides", [])
        if o["matcher"]["id"] == "byName"
        for prop in o["properties"]
        if prop["id"] == "color"
    }


def colour(panel: dict[str, Any], series: str) -> str | None:
    """The colour the panel's overrides give a series, by its name or by a pattern."""
    for override in panel.get("fieldConfig", {}).get("overrides", []):
        matcher = override["matcher"]
        named = matcher["id"] == "byName" and matcher["options"] == series
        matched = matcher["id"] == "byRegexp" and re.search(matcher["options"].strip("/"), series)
        for prop in override["properties"]:
            if (named or matched) and prop["id"] == "color":
                return str(prop["value"]["fixedColor"])
    return None


def prometheus_queries(page: dict[str, Any]) -> list[str]:
    return [
        t["expr"]
        for p in charts(page)
        for t in p["targets"]
        if t.get("datasource", p.get("datasource", {})).get("uid") == "prometheus"
    ]


@cache  # Read once: the colour test asks for it per panel.
def containers() -> dict[str, str]:
    """Each container compose starts, in its group's colour."""
    names: dict[str, str] = {}
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


def test_each_page_is_read_only_and_log_panels_read_victorialogs() -> None:
    for name, page in PAGES.items():
        # A dashboard exported from Grafana's editor comes back editable and with an id.
        assert page["editable"] is False and "id" not in page, name
        assert page["uid"] == name
    logs = [p for page in PAGES.values() for p in charts(page) if uid(p) == "logs"]
    assert logs and all(p["datasource"]["type"] == "victoriametrics-logs-datasource" for p in logs)


def test_every_link_opens_a_page_and_every_page_has_the_same_link_bar() -> None:
    bar = PAGES["home"]["links"]
    assert {link["url"] for link in bar} >= {f"/d/{name}" for name in PAGES}
    for name, page in PAGES.items():
        assert page["links"] == bar, name
        for opened in re.findall(r'"/d/([^"/?]*)', json.dumps(page)):
            assert opened in PAGES, name


@every_page
def test_every_panel_reads_a_provisioned_source(page: dict[str, Any]) -> None:
    ids = [p["id"] for p in panels(page)]
    assert len(ids) == len(set(ids))
    for panel in charts(page):
        assert uid(panel) in SOURCES, panel["title"]
        assert panel["targets"], panel["title"]
        refs = [t["refId"] for t in panel["targets"]]
        assert len(refs) == len(set(refs)), panel["title"]
        for target in panel["targets"]:
            assert target["datasource"]["uid"] in SOURCES, panel["title"]


@every_page
def test_no_two_panels_overlap_and_all_fit_the_width(page: dict[str, Any]) -> None:

    def place(group: list[dict[str, Any]]) -> None:
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
    place(page["panels"])
    for row in page["panels"]:
        if row["type"] == "row" and row["collapsed"]:
            assert all(p["gridPos"]["y"] > row["gridPos"]["y"] for p in row["panels"])
            place(row["panels"])
        elif row["type"] == "row":
            assert row["panels"] == []


def test_groups_containers_and_rules_wear_one_colour_on_every_panel() -> None:
    every = [p for page in PAGES.values() for p in charts(page)]
    by_container = [p for p in every if any(colour(p, name) for name in containers())]
    assert by_container
    for panel in by_container:
        assert {name: colour(panel, name) for name in containers()} == containers(), panel["title"]
    for panel in every:
        for group, its in GROUPS.items():
            assert colour(panel, group) in (None, its), panel["title"]
    rules = next(p for p in every if p["title"] == "Rules firing, by hour")
    assert named_colours(rules) == levels()


@every_page
def test_tiles_wear_a_state_colour_and_unlisted_series_gray(page: dict[str, Any]) -> None:
    for panel in charts(page):
        defaults = panel.get("fieldConfig", {}).get("defaults", {})
        if panel["type"] in ("stat", "gauge"):
            worn = {s["color"] for s in defaults["thresholds"]["steps"]}
            filled = panel["options"].get("colorMode", "").startswith("background")
            # A filled tile in the text colour is a white block.
            plain = set() if filled or panel["type"] == "gauge" else {"text"}
            assert worn <= {NOW, WEEK, FINE, NEUTRAL} | plain, panel["title"]
            assert set(named_colours(panel).values()) <= {NOW, WEEK}, panel["title"]
        elif defaults.get("color", {}).get("mode") != "palette-classic":
            # Past a line per container, each its own colour, a series no list names is gray.
            assert defaults.get("color", GRAY) == GRAY, panel["title"]


@every_page
def test_prometheus_queries_name_metrics_as_alloy_sends_them(page: dict[str, Any]) -> None:
    for query in prometheus_queries(page):
        bare = re.sub(r'"[^"]*"', '""', query)
        for name in re.findall(r"\b[a-zA-Z_:][a-zA-Z0-9_:]*\b", bare):
            assert MIXED_CASE.get(name.lower(), name) == name, query
        for left, right in ("()", "[]", "{}"):
            assert bare.count(left) == bare.count(right), query
    # Grafana's legends are `{{label}}`; a single-braced `{label}` shows as text.
    for panel in charts(page):
        for target in panel["targets"]:
            legend = target.get("legendFormat", "")
            assert not re.search(r"(?<!\{)\{\w+\}(?!\})", legend), panel["title"]


def test_prometheus_parses_every_query(tmp_path: Path) -> None:
    # Grafana shows a query Prometheus cannot parse only as an error on its panel. Grafana fills in
    # its duration macros before Prometheus sees the query.
    macros = re.compile(r"\$__(range|rate_interval|interval)\b")
    queries = [macros.sub("1h", q) for page in PAGES.values() for q in prometheus_queries(page)]
    rules = [{"record": f"page:{i}", "expr": q} for i, q in enumerate(queries)]
    (tmp_path / "pages.yml").write_text(json.dumps({"groups": [{"name": "pages", "rules": rules}]}))
    checked = promtool("check", "rules", str(tmp_path / "pages.yml"))
    assert checked.returncode == 0, checked.stdout + checked.stderr


@every_page
def test_log_patterns_hold_no_control_characters(page: dict[str, Any]) -> None:
    # A "\b" written through a non-raw string becomes a backspace and the pattern matches nothing.
    for panel in charts(page):
        for target in panel["targets"]:
            assert target.get("expr", "").isprintable(), panel["title"]
