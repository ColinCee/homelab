"""Every alert rule (stacks/observability/openobserve/alerts.toml) meets the alert standard in
docs/observability.md, Alertmanager carries out its lifecycle, and apply.py makes OpenObserve
match the rules and the Home dashboard through its API."""

import copy
import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml

from stacks.observability.openobserve import apply

STACK = Path(__file__).parent.parent / "stacks/observability"
CONFIG = tomllib.loads((STACK / "openobserve/alerts.toml").read_text(encoding="utf-8"))
RULES = CONFIG["rules"]
ALERTMANAGER = yaml.safe_load((STACK / "alertmanager/alertmanager.yml").read_text(encoding="utf-8"))
TEMPLATE = (STACK / "alertmanager/discord.tmpl").read_text(encoding="utf-8")
DASHBOARDS = "http://beelink.tail4f465e.ts.net:5080/web/dashboards"


def flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize("uid", RULES)
def test_a_rule_says_its_level_what_broke_why_it_matters_and_what_to_do(uid: str) -> None:
    rule = RULES[uid]
    assert rule["level"] in ("now", "week")
    assert len(rule["title"].split()) <= 8, "say the symptom in a few words"
    assert rule["measure"] and "unit" in rule
    # A sentence each, not a placeholder.
    assert len(rule["harm"].split()) >= 6 and len(rule["action"].split()) >= 6
    apply.seconds(rule["for"])
    assert rule.get("dashboard", CONFIG["dashboard"]).startswith(DASHBOARDS)


def test_levels_repeat_as_the_standard_says_and_each_post_ends_on_its_own() -> None:
    route = ALERTMANAGER["route"]
    assert route["group_by"] == ["..."] and route["repeat_interval"] == "24h"
    assert route["routes"] == [{"matchers": ['level="now"'], "repeat_interval": "4h"}]
    # Over a missed evaluation or two; the hold itself is in the query.
    assert apply.seconds(ALERTMANAGER["global"]["resolve_timeout"]) <= apply.seconds(apply.HOLD)


def test_beelink_metrics_stopped_holds_back_every_other_alert() -> None:
    assert "beelink-metrics-stopped" in RULES
    assert ALERTMANAGER["inhibit_rules"] == [
        {
            "source_matchers": ['alertname="beelink-metrics-stopped"'],
            "target_matchers": ['alertname!="beelink-metrics-stopped"'],
        }
    ]


def test_only_a_firing_act_now_post_mentions_colin() -> None:
    mention = re.search(r'define "homelab.mention" \}\}(.*?)\{\{ end \}\}\{\{ end \}\}', TEMPLATE)
    assert mention and '(eq .Status "firing") (eq .CommonLabels.level "now")' in mention[1]
    assert ALERTMANAGER["route"]["receiver"] == "discord"
    discord = next(r for r in ALERTMANAGER["receivers"] if r["name"] == "discord")
    assert discord["discord_configs"][0]["send_resolved"] is True


def test_no_data_is_kept_for_metrics_alloy_does_not_make() -> None:
    # A gone Alloy metric is "Beelink metrics stopped"; a textfile metric's writer can stop alone.
    for uid, rule in RULES.items():
        if "no_data" in rule:
            assert re.search(r"homelab_|node_textfile", rule["no_data"]), uid
            window = re.search(r"\[(\d+[mh])\]", rule["no_data"])
            # Longer than the inhibit's own window, so it never fires before it.
            assert window and apply.seconds(window[1]) > 5 * 60, uid
        else:
            assert not re.search(r"homelab_(backup|pc_backup|probe)", rule["promql"]), uid


def test_a_per_series_rule_alerts_per_label_it_names() -> None:
    for uid, rule in RULES.items():
        grouped = re.findall(r"^\(?\w+ by \(([\w, ]+)\)", flat(rule["promql"]))
        assert rule.get("series", []) == [s.strip() for g in grouped[:1] for s in g.split(",")], uid


def test_each_alert_is_filed_under_a_metric_its_query_reads() -> None:
    for rule in RULES.values():
        assert apply.stream(rule["promql"]) in rule["promql"]


class FakeOpenObserve:
    """The parts of OpenObserve's API apply.py calls, in memory."""

    def __init__(self) -> None:
        self.templates: dict[str, dict] = {}
        self.destinations: dict[str, dict] = {}
        self.folders: dict[str, str] = {}
        self.alerts: dict[str, dict] = {}
        self.calls: list[str] = []
        self.refuse = ""
        self.failing_query = ""
        # Every metric the rules read except the backup's size.
        self.streams = {apply.stream(r["promql"]) for r in RULES.values()} | {"up"}
        self.streams -= {"homelab_backup_repository_bytes"}

    def __call__(self, method: str, path: str, body: object = None) -> object:
        self.calls.append(f"{method} {path}")
        if self.refuse and self.refuse in f"{method} {path}":
            raise apply.Refused(f"{method} {path}: 400")
        body = copy.deepcopy(body)
        if path == "/api/default/streams?type=metrics":
            return {"list": [{"name": n} for n in self.streams]}
        if path.startswith("/api/default/prometheus/api/v1/query?"):
            if self.failing_query and self.failing_query in path:
                raise apply.Refused(f"{method} {path}: 400 bad query")
            return {"status": "success", "data": {"result": []}}
        if m := re.fullmatch(r"/api/default/alerts/(templates|destinations)(?:/(.+))?", path):
            store = getattr(self, m[1])
            if method == "GET":
                return list(copy.deepcopy(store).values())
            if method == "DELETE":
                if m[1] == "destinations":
                    assert all(m[2] not in a["destinations"] for a in self.alerts.values())
                del store[m[2]]
            else:
                assert isinstance(body, dict)
                store[body["name"]] = body
            return {"code": 200}
        if path == "/api/v2/default/folders/alerts":
            if method == "POST":
                assert isinstance(body, dict)
                self.folders[str(len(self.folders) + 1)] = body["name"]
                return {"folderId": str(len(self.folders))}
            return {"list": [{"folderId": k, "name": v} for k, v in self.folders.items()]}
        if m := re.fullmatch(r"/api/v2/default/alerts(?:\?folder=(\w+)|/(\w+))?", path):
            folder, alert_id = m[1], m[2]
            if method == "GET" and alert_id:
                # As OpenObserve does, a false or empty field reads back as missing.
                alert = copy.deepcopy(self.alerts[alert_id])
                if not alert["query_condition"]["promql_multi_alert"]:
                    del alert["query_condition"]["promql_multi_alert"]
                return alert
            if method == "GET":
                return {
                    "list": [
                        {"alert_id": k, "name": a["name"], "folder_id": a["folder_id"]}
                        for k, a in self.alerts.items()
                        if a["folder_id"] == folder
                    ]
                }
            if method == "DELETE":
                del self.alerts[alert_id]
                return {"code": 200}
            assert isinstance(body, dict)
            if method == "POST":
                alert_id = f"a{len(self.calls)}"
                body |= {"id": alert_id, "folder_id": folder, "org_id": "default"}
            # OpenObserve refuses these in a name.
            assert not re.search(r"[:#?&%'\"\s]", body["name"]), body["name"]
            assert body["destinations"] and all(
                d in self.destinations for d in body["destinations"]
            )
            assert body["template"] in self.templates
            assert body["stream_name"] in self.streams
            self.alerts[alert_id] = body
            return {"code": 200}
        raise AssertionError(f"unexpected call {method} {path}")


def applied(config: dict, fake: FakeOpenObserve | None = None) -> tuple[FakeOpenObserve, list[str]]:
    fake = fake or FakeOpenObserve()
    return fake, apply.apply(fake, config)


def by_title(fake: FakeOpenObserve) -> dict[str, dict]:
    return {a["context_attributes"]["title"]: a for a in fake.alerts.values()}


def test_every_rule_posts_each_minute_to_alertmanager_alone() -> None:
    fake, changes = applied(CONFIG)
    assert len(fake.alerts) == len(RULES) and "created folder homelab" in changes
    assert set(fake.destinations) == {"homelab-alertmanager"}
    assert (
        fake.destinations["homelab-alertmanager"]["url"] == "http://alertmanager:9093/api/v2/alerts"
    )
    for alert in fake.alerts.values():
        assert alert["destinations"] == ["homelab-alertmanager"]
        assert alert["trigger_condition"]["silence"] == 0 and not alert["notify_on_recovery"]


def test_the_old_destinations_and_templates_are_removed() -> None:
    fake = FakeOpenObserve()
    fake.destinations |= {
        n: {"name": n} for n in ("homelab-shadow", "homelab-kaizen", "homelab-discord")
    }
    fake.templates |= {
        n: {"name": n} for n in ("homelab-kaizen", "homelab-kaizen-kind", "someone-elses")
    }
    changes = applied(CONFIG, fake)[1]
    for name in ("homelab-shadow", "homelab-kaizen", "homelab-discord"):
        assert f"deleted destination {name}" in changes
    assert set(fake.templates) == {
        "homelab-alertmanager",
        "homelab-alertmanager-name",
        "someone-elses",
    }


def test_a_second_run_changes_nothing() -> None:
    fake, _ = applied(CONFIG)
    before = len(fake.calls)
    assert applied(CONFIG, fake)[1] == []
    assert not [c for c in fake.calls[before:] if not c.startswith("GET")]


def test_a_changed_rule_updates_only_its_alert() -> None:
    fake, _ = applied(CONFIG)
    changed = copy.deepcopy(CONFIG)
    changed["rules"]["high-disk"]["threshold"] = 85
    assert applied(changed, fake)[1] == ["updated alert high-disk"]
    assert (
        by_title(fake)["Beelink disk filling up"]["query_condition"]["promql_condition"]["value"]
        == 85
    )


def test_a_rule_git_removed_is_deleted_and_others_are_left_alone() -> None:
    fake, _ = applied(CONFIG)
    fake.alerts["x"] = {"name": "by-hand", "folder_id": "other", "destinations": []}
    fewer = copy.deepcopy(CONFIG)
    del fewer["rules"]["ssd-worn"]
    assert applied(fewer, fake)[1] == ["deleted alert ssd-worn"]
    assert "x" in fake.alerts


def test_an_alert_on_a_metric_with_no_data_yet_is_filed_under_up_until_it_has_some() -> None:
    fake, _ = applied(CONFIG)
    assert by_title(fake)["Backup storage nearly full"]["stream_name"] == "up"
    fake.streams.add("homelab_backup_repository_bytes")
    assert applied(CONFIG, fake)[1] == ["updated alert backup-storage-high"]


def test_a_refused_call_stops_the_run() -> None:
    fake = FakeOpenObserve()
    fake.refuse = "POST /api/v2/default/alerts"
    with pytest.raises(apply.Refused):
        applied(CONFIG, fake)


def test_a_rule_whose_query_fails_is_named() -> None:
    fake, _ = applied(CONFIG)
    assert apply.failed_queries(fake, CONFIG) == []
    fake.failing_query = "homelab_ssd_percentage_used"
    assert [f.split(":")[0] for f in apply.failed_queries(fake, CONFIG)] == ["ssd-worn"]


def test_a_level_outside_the_standard_stops_the_run() -> None:
    wrong = copy.deepcopy(CONFIG)
    wrong["rules"]["ssd-worn"]["level"] = "later"
    with pytest.raises(SystemExit):
        applied(wrong)


def filled(template: str, alert: dict, labels: dict[str, str]) -> list:
    """The body OpenObserve sends: each placeholder becomes its value, JSON-escaped."""
    values = {**alert["context_attributes"], **labels, "alert_name": alert["name"]}
    values |= {"alert_agg_value": "7"}
    return json.loads(re.sub(r"\{(\w+)\}", lambda m: json.dumps(values[m[1]])[1:-1], template))


def test_a_per_series_alert_reaches_alertmanager_with_its_labels_and_post() -> None:
    fake, _ = applied(CONFIG)
    alert = by_title(fake)["A container keeps restarting"]
    assert alert["query_condition"]["promql_multi_alert"]
    [body] = filled(fake.templates[alert["template"]]["body"], alert, {"name": 'we"b'})
    assert body["labels"] == {"alertname": "restart-loop", "level": "now", "name": 'we"b'}
    assert body["annotations"]["value"] == "7" and body["annotations"]["dashboard"].startswith(
        DASHBOARDS
    )
    assert set(body["annotations"]) == {
        "title",
        "measure",
        "unit",
        "harm",
        "action",
        "dashboard",
        "value",
    }
    # No end time: Alertmanager ends it resolve_timeout after the last post.
    assert "endsAt" not in body


def test_a_no_data_rule_fires_on_a_missing_metric() -> None:
    fake, _ = applied(CONFIG)
    condition = by_title(fake)["PC backup stopped arriving"]["query_condition"]
    assert condition["promql"].endswith(
        ") > 50 or on() (absent_over_time(homelab_pc_backup_newest_timestamp_seconds[15m])) * -1"
    )
    assert condition["promql_condition"] == {"column": "value", "operator": ">=", "value": -1}


def test_for_and_the_hold_are_in_the_query() -> None:
    fake, _ = applied(CONFIG)
    alerts = by_title(fake)
    ram = alerts["Beelink running out of memory"]
    # Held 5 minutes at some time in the last 10: a dip is no clear, a spike no onset.
    assert ram["pending_period_sec"] == 0 and ram["keep_firing_for"] == 0
    assert ram["query_condition"]["promql"].startswith("max_over_time((min_over_time((round(")
    assert ram["query_condition"]["promql"].endswith("[5m:1m]))[10m:1m])")
    site = alerts["Flight Tracker site down"]["query_condition"]["promql"]
    assert site.startswith("min_over_time((max_over_time((max(probe_success")
    # No `for`: only the hold.
    worn = alerts["Beelink metrics stopped"]["query_condition"]["promql"]
    assert worn.startswith("max_over_time((absent_over_time(")


def test_a_unit_keeps_its_leading_space() -> None:
    fake, _ = applied(CONFIG)
    assert by_title(fake)["Nightly backup didn't run"]["context_attributes"]["unit"] == " h ago"


IN_GIT = apply.read_dashboards(STACK / "openobserve/dashboards")


class FakeDashboards:
    """OpenObserve's dashboard and dashboard folder API, in memory."""

    def __init__(self) -> None:
        self.folders = {"default": "default"}
        self.dashboards: dict[str, dict] = {}
        self.calls: list[str] = []

    def __call__(self, method: str, path: str, body: object = None) -> object:
        self.calls.append(f"{method} {path}")
        if path == "/api/v2/default/folders/dashboards":
            if method == "POST":
                assert isinstance(body, dict)
                self.folders[str(len(self.folders))] = body["name"]
                return {"folderId": str(len(self.folders) - 1)}
            return {"list": [{"folderId": k, "name": v} for k, v in self.folders.items()]}
        m = re.fullmatch(r"/api/default/dashboards(?:/(\d+))?\?folder=(\w+)(?:&hash=(\w+))?", path)
        assert m, path
        dashboard_id, folder, hash_ = m.groups()
        if method == "GET":
            return {
                "dashboards": [
                    copy.deepcopy(d) for d in self.dashboards.values() if d["folder_id"] == folder
                ]
            }
        if method == "DELETE":
            assert self.dashboards[dashboard_id]["folder_id"] == folder
            del self.dashboards[dashboard_id]
            return {"code": 200}
        assert isinstance(body, dict)
        if method == "PUT":
            # OpenObserve refuses an update that does not name the version it replaces.
            assert hash_ == self.dashboards[dashboard_id]["hash"]
        else:
            dashboard_id = str(1000 + len(self.calls))
        self.dashboards[dashboard_id] = {
            "dashboard_id": dashboard_id,
            "folder_id": folder,
            "title": body["title"],
            "hash": str(len(self.calls)),
            "v8": self.read_back(body) | {"dashboardId": dashboard_id, "owner": "root"},
        }
        return {"code": 200}

    @staticmethod
    def read_back(body: dict) -> dict:
        """As OpenObserve stores it: unset fields come back as null and numbers as floats."""
        stored = copy.deepcopy(body)
        for tab in stored["tabs"]:
            for panel in tab["panels"]:
                panel["config"] |= {"legends_position": None, "base_map": None}
                panel["config"]["decimals"] = float(panel["config"].get("decimals", 2))
                for query in panel["queries"]:
                    query["vrlFunctionQuery"] = None
        return stored


def test_the_dashboards_are_made_once_and_then_match() -> None:
    fake = FakeDashboards()
    assert apply.apply_dashboards(fake, IN_GIT) == [
        "created dashboard folder homelab",
        "created dashboard Home",
    ]
    before = len(fake.calls)
    assert apply.apply_dashboards(fake, IN_GIT) == []
    assert all(c.startswith("GET") for c in fake.calls[before:])


def test_a_changed_dashboard_is_updated_and_a_removed_one_deleted() -> None:
    fake = FakeDashboards()
    apply.apply_dashboards(fake, IN_GIT)
    by_hand = {"dashboard_id": "9", "folder_id": "default", "title": "By hand", "hash": "1"}
    fake.dashboards["9"] = by_hand
    changed = copy.deepcopy(IN_GIT)
    changed["Home"]["tabs"][0]["panels"][1]["title"] = "Flight tracker site"
    assert apply.apply_dashboards(fake, changed) == ["updated dashboard Home"]
    assert apply.apply_dashboards(fake, {}) == ["deleted dashboard Home"]
    assert [d["title"] for d in fake.dashboards.values()] == ["By hand"]


def queries(dashboard: dict, kind: str) -> list[str]:
    return [
        q["query"]
        for tab in dashboard["tabs"]
        for panel in tab["panels"]
        if panel["queryType"] == kind
        for q in panel["queries"]
    ]


def test_dashboard_queries_name_metrics_in_lower_case() -> None:
    # OpenObserve keeps metric names in lower case: node_memory_MemAvailable_bytes finds nothing.
    for query in queries(IN_GIT["Home"], "promql"):
        bare = re.sub(r'"[^"]*"', "", query)
        assert not re.search(r"\b[a-z]+_\w*[A-Z]\w*", bare), query


def test_log_patterns_hold_no_control_characters() -> None:
    # A "\b" written through a non-raw string becomes a backspace and the pattern matches nothing.
    for query in queries(IN_GIT["Home"], "sql"):
        assert query.isprintable(), query


def test_no_two_panels_on_a_tab_overlap_and_all_fit_its_width() -> None:
    for tab in IN_GIT["Home"]["tabs"]:
        cells: set[tuple[int, int]] = set()
        for panel in tab["panels"]:
            box = panel["layout"]
            assert box["x"] + box["w"] <= 192, panel["title"]
            mine = {
                (x, y)
                for x in range(box["x"], box["x"] + box["w"])
                for y in range(box["y"], box["y"] + box["h"])
            }
            assert not cells & mine, (tab["name"], panel["title"])
            cells |= mine


# Home's colours (.github/skills/dashboards/SKILL.md, "Colour and order"). OpenObserve takes one
# colour for both themes: these dataviz steps pass its checks on the light and the dark surface.
GROUPS = {"Monitoring": "#2a78d6", "System": "#199e70", "Apps": "#d95926"}
NOW, WEEK, NEUTRAL = "#d03b3b", "#fab219", "#898781"
PANELS = [p for tab in IN_GIT["Home"]["tabs"] for p in tab["panels"]]


def series_colours(panel: dict) -> dict[str, str]:
    mappings = panel["config"].get("color", {}).get("colorBySeries", [])
    return {m["value"]: m["color"] for m in mappings}


def containers() -> dict[str, str]:
    """Each container compose starts, in its group's colour."""
    names = {}
    for compose in STACK.parent.glob("*/compose.yaml"):
        services = yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]
        for service, spec in services.items():
            name = spec.get("container_name", f"{compose.parent.name}-{service}-1")
            names[name] = GROUPS["Monitoring" if compose.parent.name == "observability" else "Apps"]
    return names


def test_groups_containers_and_rules_wear_one_colour_on_every_tab() -> None:
    by_container = [p for p in PANELS if set(series_colours(p)) & set(containers())]
    assert len(by_container) == 4
    for panel in by_container:
        assert series_colours(panel) == containers(), panel["title"]
    for panel in PANELS:
        for group in set(series_colours(panel)) & set(GROUPS):
            assert series_colours(panel)[group] == GROUPS[group], panel["title"]
    levels = {uid: NOW if rule["level"] == "now" else WEEK for uid, rule in RULES.items()}
    rules = next(p for p in PANELS if p["title"] == "Rules firing, by hour")
    assert series_colours(rules) == levels


def test_tiles_are_red_or_amber_and_unlisted_series_gray() -> None:
    for panel in PANELS:
        config = panel["config"]
        for mapping in config.get("mappings", []):
            assert mapping["color"] in ("", NOW, WEEK), panel["title"]
        assert config.get("sparkline", {}).get("color", NEUTRAL) == NEUTRAL, panel["title"]
        if "color" in config:
            # A series no list names, like a removed container, is drawn gray.
            assert config["color"]["mode"] == "fixed", panel["title"]
            assert config["color"]["fixedColor"] == [NEUTRAL], panel["title"]
