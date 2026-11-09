"""Runs each rule's query once, then makes OpenObserve's alerts, their templates and destination
match alerts.toml, and its dashboards match the dashboards/ folder beside it.

    apply.py alerts.toml

Alerts live in the `homelab` alert folder, named by their key in alerts.toml; dashboards in the
`homelab` dashboard folder, by their title. One there that Git no longer has is deleted, and so are
`homelab-` templates and destinations nothing uses. Prints each change; exits non-zero on the first
refused call or a query that fails. Reads OPENOBSERVE_URL, OPENOBSERVE_USER and
OPENOBSERVE_PASSWORD."""

import base64
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ORG = "default"
FOLDER = "homelab"
OWNED = "homelab-"
DESTINATION = OWNED + "alertmanager"
# Alertmanager on the stack's network, the private host OpenObserve allows (ZO_SSRF_ALLOWED_HOSTS).
ALERTMANAGER = "http://alertmanager:9093/api/v2/alerts"
# The value a rule's `no_data` query gives when its metric is gone, shown as "no data".
NO_DATA = -1
LEVELS = ("now", "week")
# How long a rule keeps firing after its condition clears, so a dip shorter than this is no clear.
HOLD = "10m"
# A rule's fields that reach its post, as context attributes: the template's `{name}`s.
POST = ("level", "title", "measure", "unit", "harm", "action", "dashboard")

Call = Callable[[str, str, object], Any]


class Refused(Exception):
    pass


def client(url: str, user: str, password: str) -> Call:
    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def call(method: str, path: str, body: object = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            url + path,
            data=data,
            method=method,
            headers={"Authorization": auth, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as error:
            raise Refused(f"{method} {path}: {error.code} {error.read().decode()}") from None
        return json.loads(text) if text else None

    return call


def seconds(duration: str) -> int:
    match = re.fullmatch(r"(\d+)([smh])", duration)
    if not match:
        raise SystemExit(f"{duration}: not a duration like 10m")
    return int(match[1]) * {"s": 1, "m": 60, "h": 3600}[match[2]]


def template_name(series: list[str]) -> str:
    return OWNED + "-".join(["alertmanager", *series])


def template_body(series: list[str]) -> str:
    """Alertmanager's alert list, of `{placeholder}`s OpenObserve fills JSON-escaped. With no
    endsAt, Alertmanager ends the alert `resolve_timeout` after its last post."""
    labels = {"alertname": "{alert_name}", "level": "{level}"}
    labels |= {label: f"{{{label}}}" for label in series}
    annotations = {field: f"{{{field}}}" for field in POST if field != "level"}
    return json.dumps(
        [{"labels": labels, "annotations": annotations | {"value": "{alert_agg_value}"}}]
    )


def stream(promql: str) -> str:
    """The first metric the query reads: OpenObserve files each alert under one stream."""
    bare = re.sub(r"\{[^}]*\}|\[[^\]]*\]|\b(?:by|on|without|ignoring)\s*\([^)]*\)", " ", promql)
    names = re.findall(r"(?<![\w.])([a-zA-Z_:][a-zA-Z0-9_:]*)\b(?!\s*\()", bare)
    return next(n for n in names if n not in {"and", "or", "unless", "bool", "offset", "e"})


def query(rule: Mapping[str, Any], held: bool = True) -> tuple[str, str, float]:
    """The PromQL OpenObserve runs, and the condition on its value. Held, `for` and the hold are in
    the query: OpenObserve's own pending time restarts after any dip, and its keep_firing_for never
    sends the resolve. It fires while the value stayed past the threshold for `for` at any time in
    the last 10 minutes, so a dip under that is no clear and a spike is no onset."""
    promql = " ".join(rule["promql"].split())
    operator, threshold = rule["operator"], rule["threshold"]
    if held:
        low, high = ("max", "min") if operator.startswith("<") else ("min", "max")
        if seconds(rule["for"]):
            promql = f"{low}_over_time(({promql})[{rule['for']}:1m])"
        promql = f"{high}_over_time(({promql})[{HOLD}:1m])"
    if "no_data" in rule:
        no_data = " ".join(rule["no_data"].split())
        promql = f"({promql}) {operator} {threshold} or on() ({no_data}) * {NO_DATA}"
        operator, threshold = ">=", NO_DATA
    return promql, operator, threshold


def desired(config: Mapping[str, Any]) -> tuple[dict, dict, list[dict]]:
    """Templates, destinations and alerts by name, as OpenObserve's API takes them."""
    templates: dict[str, dict] = {}
    alerts = []
    for uid, rule in config["rules"].items():
        if rule["level"] not in LEVELS:
            raise SystemExit(f"{uid}: level is {rule['level']!r}, not one of {LEVELS}")
        series = rule.get("series", [])
        template = template_name(series)
        templates[template] = {"body": template_body(series)}
        promql, operator, threshold = query(rule)
        context = {field: " ".join(str(rule.get(field, "")).split()) for field in POST}
        # A unit's leading space is part of it ("5 h ago", "93%").
        context["unit"] = rule["unit"]
        context["dashboard"] = rule.get("dashboard", config["dashboard"])
        alerts.append(
            {
                # OpenObserve refuses spaces and quotes in a name, so the title is a context field.
                "name": uid,
                "stream_type": "metrics",
                "stream_name": stream(rule["promql"]),
                "is_real_time": False,
                "enabled": True,
                "description": rule["title"],
                "query_condition": {
                    "type": "promql",
                    "promql": promql,
                    "promql_condition": {
                        "column": "value",
                        "operator": operator,
                        "value": threshold,
                    },
                    "promql_multi_alert": bool(series),
                },
                # Posted every minute while it fires: Alertmanager repeats, clears and holds back.
                "trigger_condition": {
                    "period": 1,
                    "operator": ">=",
                    "threshold": 1,
                    "frequency": 1,
                    "frequency_type": "minutes",
                    "silence": 0,
                },
                "pending_period_sec": 0,
                "keep_firing_for": 0,
                "notify_on_recovery": False,
                "destinations": [DESTINATION],
                "template": template,
                "context_attributes": context,
            }
        )
    destinations = {
        DESTINATION: {
            "url": ALERTMANAGER,
            "method": "post",
            "type": "http",
            "template": template_name([]),
            "headers": {},
        }
    }
    templates.setdefault(template_name([]), {"body": template_body([])})
    return templates, destinations, alerts


def differs(want: object, have: Any) -> bool:
    """Whether `have` lacks anything in `want`. Fields OpenObserve adds are left alone, and it reads
    back an empty or false field as missing."""
    if isinstance(want, dict):
        return not isinstance(have, dict) or any(differs(v, have.get(k)) for k, v in want.items())
    if isinstance(want, list) and isinstance(have, list) and len(want) == len(have):
        return any(differs(w, h) for w, h in zip(want, have, strict=True))
    if want in (None, "", False, 0) and have in (None, "", False, 0):
        return False
    return want != have


def apply(call: Call, config: Mapping[str, Any]) -> list[str]:
    templates, destinations, alerts = desired(config)
    changes: list[str] = []
    # OpenObserve refuses an alert on a stream it has no data for yet, as for a new metric; such an
    # alert is filed under `up` until a later run finds its own.
    streams = {s["name"] for s in call("GET", f"/api/{ORG}/streams?type=metrics", None)["list"]}
    for alert in alerts:
        if alert["stream_name"] not in streams:
            alert["stream_name"] = "up"

    def put(kind: str, path: str, existing: Mapping[str, dict], name: str, want: dict) -> None:
        if name not in existing:
            call("POST", path, {"name": name, **want})
            changes.append(f"created {kind} {name}")
        elif differs(want, existing[name]):
            call("PUT", f"{path}/{name}", {**existing[name], **want, "name": name})
            changes.append(f"updated {kind} {name}")

    have_templates = {t["name"]: t for t in call("GET", f"/api/{ORG}/alerts/templates", None)}
    for name, want in templates.items():
        put(
            "template",
            f"/api/{ORG}/alerts/templates",
            have_templates,
            name,
            {"type": "http", **want},
        )
    have_destinations = {d["name"]: d for d in call("GET", f"/api/{ORG}/alerts/destinations", None)}
    for name, want in destinations.items():
        put("destination", f"/api/{ORG}/alerts/destinations", have_destinations, name, want)

    folders = call("GET", f"/api/v2/{ORG}/folders/alerts", None)["list"]
    folder = next((f["folderId"] for f in folders if f["name"] == FOLDER), None)
    if folder is None:
        folder = call(
            "POST", f"/api/v2/{ORG}/folders/alerts", {"name": FOLDER, "description": "From Git"}
        )["folderId"]
        changes.append(f"created folder {FOLDER}")
    listed = call("GET", f"/api/v2/{ORG}/alerts?folder={folder}", None)["list"]
    have_alerts = {a["name"]: a["alert_id"] for a in listed if a.get("folder_id") == folder}
    for want in alerts:
        name = want["name"]
        if name not in have_alerts:
            call("POST", f"/api/v2/{ORG}/alerts?folder={folder}", want)
            changes.append(f"created alert {name}")
            continue
        have = call("GET", f"/api/v2/{ORG}/alerts/{have_alerts[name]}", None)
        if differs(want, have):
            call("PUT", f"/api/v2/{ORG}/alerts/{have_alerts[name]}", {**have, **want})
            changes.append(f"updated alert {name}")
    for name in sorted(set(have_alerts) - {a["name"] for a in alerts}):
        call("DELETE", f"/api/v2/{ORG}/alerts/{have_alerts[name]}", None)
        changes.append(f"deleted alert {name}")

    # A destination or template still in use cannot be deleted, so these go after the alerts.
    for name in sorted(
        n for n in have_destinations if n.startswith(OWNED) and n not in destinations
    ):
        call("DELETE", f"/api/{ORG}/alerts/destinations/{name}", None)
        changes.append(f"deleted destination {name}")
    for name in sorted(n for n in have_templates if n.startswith(OWNED) and n not in templates):
        call("DELETE", f"/api/{ORG}/alerts/templates/{name}", None)
        changes.append(f"deleted template {name}")
    return changes


def failed_queries(call: Call, config: Mapping[str, Any]) -> list[str]:
    """Rules whose query OpenObserve cannot run: as an alert it would only be marked `error`, and
    send nothing."""
    failed = []
    for uid, rule in config["rules"].items():
        promql = query(rule)[0]
        path = f"/api/{ORG}/prometheus/api/v1/query?" + urllib.parse.urlencode({"query": promql})
        try:
            answer = call("GET", path, None)
        except Refused as error:
            failed.append(f"{uid}: {str(error)[:300]}")
            continue
        if answer.get("status") != "success":
            failed.append(f"{uid}: {json.dumps(answer)[:300]}")
    return failed


def apply_dashboards(call: Call, dashboards: Mapping[str, dict]) -> list[str]:
    """Makes the `homelab` dashboard folder hold these dashboards, by title, and no others."""
    changes: list[str] = []
    folders = call("GET", f"/api/v2/{ORG}/folders/dashboards", None)["list"]
    folder = next((f["folderId"] for f in folders if f["name"] == FOLDER), None)
    if folder is None:
        folder = call(
            "POST", f"/api/v2/{ORG}/folders/dashboards", {"name": FOLDER, "description": "From Git"}
        )["folderId"]
        changes.append(f"created dashboard folder {FOLDER}")
    path = f"/api/{ORG}/dashboards"
    listed = call("GET", f"{path}?folder={folder}", None)["dashboards"]
    have = {d["title"]: d for d in listed}
    for title, want in dashboards.items():
        if title not in have:
            call("POST", f"{path}?folder={folder}", want)
            changes.append(f"created dashboard {title}")
        elif differs(want, have[title]["v8"]):
            # The hash says which version this replaces; OpenObserve refuses an update without it.
            found = have[title]
            where = f"{found['dashboard_id']}?folder={folder}&hash={found['hash']}"
            call("PUT", f"{path}/{where}", want)
            changes.append(f"updated dashboard {title}")
    for title in sorted(set(have) - set(dashboards)):
        call("DELETE", f"{path}/{have[title]['dashboard_id']}?folder={folder}", None)
        changes.append(f"deleted dashboard {title}")
    return changes


def read_dashboards(folder: Path) -> dict[str, dict]:
    # A wrong path would read as no dashboards and delete them all.
    if not folder.is_dir():
        raise SystemExit(f"{folder}: no such folder")
    dashboards = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]
    titles = [d["title"] for d in dashboards]
    if len(set(titles)) != len(titles):
        raise SystemExit(f"{folder}: two dashboards share a title")
    return {d["title"]: d for d in dashboards}


def wait_until_ready(call: Call, timeout: float = 90) -> None:
    """Until OpenObserve answers and holds the `up` stream that alerts fall back to (first deploy).
    Under deploy's 120 s `up --wait`, so this message is the one in the log."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            streams = call("GET", f"/api/{ORG}/streams?type=metrics", None)["list"]
            if any(s["name"] == "up" for s in streams):
                return
        except OSError, Refused:
            pass
        if time.monotonic() > deadline:
            raise SystemExit(f"OpenObserve had no `up` metric after {timeout:.0f} s")
        time.sleep(3)


def main() -> None:
    with open(sys.argv[1], "rb") as file:
        config = tomllib.load(file)
    url = os.environ.get("OPENOBSERVE_URL", "http://openobserve:5080")
    call = client(
        url,
        os.environ.get("OPENOBSERVE_USER", "root@homelab.internal"),
        os.environ["OPENOBSERVE_PASSWORD"],
    )
    wait_until_ready(call)
    # Before applying, so a rule that cannot run never replaces the one before it.
    if failed := failed_queries(call, config):
        raise SystemExit("These rules' queries fail:\n" + "\n".join(failed))
    dashboards = read_dashboards(Path(sys.argv[1]).parent / "dashboards")
    try:
        changes = apply(call, config) + apply_dashboards(call, dashboards)
    except Refused as error:
        raise SystemExit(f"OpenObserve refused {str(error)[:500]}") from None
    print("\n".join(changes) or "OpenObserve's alerts and dashboards already match", flush=True)


if __name__ == "__main__":
    main()
