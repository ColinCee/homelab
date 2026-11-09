"""OpenObserve's rules (stacks/observability/openobserve/alerts.toml) match Grafana's until the
cutover, and apply.py makes OpenObserve match them through its API."""

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
GRAFANA = {
    r["uid"]: r
    for g in yaml.safe_load(
        (STACK / "provisioning/alerting/rules.yaml").read_text(encoding="utf-8")
    )["groups"]
    for r in g["rules"]
}
ENV = {"KAIZEN_ALERT_SECRET": "s3cret", "DISCORD_PRIVATE_WEBHOOK_URL": "https://discord.test/hook"}


def flat(text: str) -> str:
    return " ".join(text.split())


def test_every_grafana_rule_has_one_with_its_title_route_and_closes() -> None:
    assert RULES.keys() == GRAFANA.keys()
    for uid, rule in RULES.items():
        grafana = GRAFANA[uid]
        assert rule["title"] == grafana["title"], uid
        outage = grafana["title"] == "Kaizen Control room unreachable"
        assert rule["route"] == ("uptime" if outage else grafana["labels"].get("route", "kaizen"))
        if "closes" in grafana["annotations"]:
            assert rule.get("closes") == grafana["annotations"]["closes"], uid
        assert rule.get("keep_firing") == grafana.get("keepFiringFor"), uid
        # Grafana's no-data alert; the backup's size is a new metric, missing when the backup stops.
        silent = grafana["noDataState"] == "OK" and uid != "backup-storage-high"
        assert ("no_data" not in rule) == silent, uid
        assert ("needs" in rule) == ("needs" in grafana["annotations"]), uid
        assert rule["for"] in (grafana["for"], "0m" if grafana["for"] == "0s" else None), uid


def test_a_chart_queries_what_its_rule_alerts_on() -> None:
    for uid, rule in RULES.items():
        if "chart" in rule:
            # The rule's own query, at most scaled to the box's unit, or its measure without a gate.
            chart, promql = flat(rule["chart"]), flat(rule["promql"])
            assert promql in chart or promql.startswith(chart), uid
            float(rule["chart_line"])


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
        # Every metric the rules read except the backup's new one.
        self.streams = {apply.stream(r["promql"]) for r in RULES.values()} | {"up"}
        self.streams -= {"homelab_backup_repository_bytes"}

    def __call__(self, method: str, path: str, body: object = None) -> object:
        self.calls.append(f"{method} {path}")
        if self.refuse and self.refuse in f"{method} {path}":
            raise apply.Refused(f"{method} {path}: 400")
        body = copy.deepcopy(body)
        if path == "/api/default/streams?type=metrics":
            return {"list": [{"name": n} for n in self.streams]}
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
                return alert | {"template": alert["template"] or ""}
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
            assert body["template"] in (None, *self.templates)
            assert body["stream_name"] in self.streams
            self.alerts[alert_id] = body
            return {"code": 200}
        raise AssertionError(f"unexpected call {method} {path}")


def applied(config: dict, fake: FakeOpenObserve | None = None) -> tuple[FakeOpenObserve, list[str]]:
    fake = fake or FakeOpenObserve()
    return fake, apply.apply(fake, config, ENV)


def by_title(fake: FakeOpenObserve) -> dict[str, dict]:
    return {a["context_attributes"]["title"]: a for a in fake.alerts.values()}


def test_until_the_cutover_every_rule_goes_only_to_the_shadow_path() -> None:
    fake, changes = applied(CONFIG)
    assert len(fake.alerts) == len(RULES) and "created folder homelab" in changes
    assert {d for a in fake.alerts.values() for d in a["destinations"]} == {"homelab-shadow"}
    assert set(fake.destinations) == {"homelab-shadow"}
    shadow = fake.destinations["homelab-shadow"]
    assert shadow["url"].endswith("/alert/openobserve/shadow")
    assert shadow["headers"] == {"Authorization": "Bearer s3cret"}
    assert all(a["trigger_condition"]["silence"] == 0 for a in fake.alerts.values())


def test_a_second_run_changes_nothing() -> None:
    fake, _ = applied(CONFIG)
    before = len(fake.calls)
    assert applied(CONFIG, fake)[1] == []
    assert not [c for c in fake.calls[before:] if not c.startswith("GET")]


def test_a_changed_rule_updates_only_its_alert() -> None:
    fake, _ = applied(CONFIG)
    changed = copy.deepcopy(CONFIG)
    changed["rules"]["high-disk"]["threshold"] = 0.85
    assert applied(changed, fake)[1] == ["updated alert high-disk"]
    condition = by_title(fake)["Beelink disk filling up"]["query_condition"]
    assert ") > 0.85 or on()" in condition["promql"]


def test_a_rule_git_removed_is_deleted_and_others_are_left_alone() -> None:
    fake, _ = applied(CONFIG)
    fake.templates["someone-elses"] = {"name": "someone-elses", "body": "{}"}
    fake.alerts["x"] = {"name": "by-hand", "folder_id": "other", "destinations": []}
    fewer = copy.deepcopy(CONFIG)
    del fewer["rules"]["ssd-worn"]
    assert applied(fewer, fake)[1] == ["deleted alert ssd-worn"]
    assert "someone-elses" in fake.templates and "x" in fake.alerts


def test_a_listed_alert_that_names_no_folder_is_not_deleted() -> None:
    fake, _ = applied(CONFIG)

    def call(method: str, path: str, body: object = None) -> object:
        got = fake(method, path, body)
        if method == "GET" and path.startswith("/api/v2/default/alerts?"):
            got["list"].append({"alert_id": "x", "name": "by-hand"})  # type: ignore[index]
        return got

    assert apply.apply(call, CONFIG, ENV) == []


def test_live_rules_go_to_their_routes_and_shadow_is_removed() -> None:
    fake, _ = applied(CONFIG)
    live = copy.deepcopy(CONFIG) | {"live": True}
    changes = applied(live, fake)[1]
    assert "deleted destination homelab-shadow" in changes
    alerts = by_title(fake)
    assert alerts["Beelink SSD wearing out"]["destinations"] == ["homelab-kaizen"]
    assert alerts["Kaizen Control room unreachable"]["destinations"] == [
        "homelab-discord",
        "homelab-kaizen",
    ]
    assert alerts["Kaizen Control room unreachable"]["trigger_condition"]["silence"] == 30
    assert alerts["Kaizen's agents spending fast"]["destinations"] == ["homelab-kaizen"]
    assert alerts["Kaizen's agents spending fast"]["trigger_condition"]["silence"] == 0
    assert fake.destinations["homelab-discord"]["url"] == "https://discord.test/hook"
    assert fake.destinations["homelab-discord"]["headers"] == {}


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


def filled(template: str, alert: dict, labels: dict[str, str]) -> dict:
    """The body OpenObserve sends: each placeholder becomes its value, JSON-escaped."""
    values = {**alert["context_attributes"], **labels, "alert_name": alert["name"]}
    values |= {"alert_status": "firing", "alert_agg_value": "7"}
    return json.loads(re.sub(r"\{(\w+)\}", lambda m: json.dumps(values[m[1]])[1:-1], template))


def test_a_per_series_alert_sends_its_labels_and_kaizens_post_fields() -> None:
    fake, _ = applied(CONFIG)
    alert = by_title(fake)["A Kaizen job ran out of memory"]
    assert alert["query_condition"]["promql_multi_alert"] and not alert["notify_on_recovery"]
    body = filled(fake.templates[alert["template"]]["body"], alert, {"kind": 'li"ght'})
    assert body["labels"] == {"kind": 'li"ght'}
    assert body["post"] | {"chart": ""} == {
        "name": "A Kaizen job ran out of memory",
        "value": "7",
        "measure": "Kills in the last 15 min",
        "trigger": "any",
        "chart": "",
        "chart_line": "0",
        "closes": "event",
        "needs": "",
    }
    assert body["status"] == "firing" and body["route"] == "kaizen"


def test_a_single_series_alert_sends_its_extra_queries_and_its_resolve() -> None:
    fake, _ = applied(CONFIG)
    alert = by_title(fake)["Kaizen's Watcher stopped running"]
    assert alert["notify_on_recovery"] and alert["template"] is None
    body = filled(fake.templates["homelab-kaizen"]["body"], alert, {})
    assert body["labels"] == {}
    assert set(json.loads(body["extra"])) == {"kaizen", "watcher", "busy", "slots"}


def test_a_no_data_rule_fires_on_a_missing_metric() -> None:
    fake, _ = applied(CONFIG)
    condition = by_title(fake)["PC backup stopped arriving"]["query_condition"]
    assert condition["promql"] == (
        "((time() - max(homelab_pc_backup_newest_timestamp_seconds)) / 3600) > 50 or on() "
        "(absent_over_time(homelab_pc_backup_newest_timestamp_seconds[5m])) * -1"
    )
    assert condition["promql_condition"] == {"column": "value", "operator": ">=", "value": -1}


def test_rules_hold_for_grafanas_for_and_keep_firing() -> None:
    fake, _ = applied(CONFIG)
    alerts = by_title(fake)
    cpu = alerts["Beelink CPU maxed out"]
    # Held 20 minutes at some time in the last 10: the hold is in the query.
    assert cpu["pending_period_sec"] == 0 and cpu["keep_firing_for"] == 0
    assert cpu["query_condition"]["promql"].startswith("(max_over_time((min_over_time((( sum(")
    assert "[20m:1m]))[10m:1m])) > 0.8 or on()" in cpu["query_condition"]["promql"]
    site = alerts["Flight Tracker site down"]["query_condition"]["promql"]
    assert site.startswith("(min_over_time((max_over_time((max(probe_success")
    assert alerts["Beelink disk filling up"]["pending_period_sec"] == 600
