"""Makes OpenObserve's alerts, their templates and destinations match alerts.toml.

    apply.py alerts.toml

Alerts live in the `homelab` folder, named by their key in alerts.toml. One there that the file
no longer has is deleted, and so are `homelab-` templates and destinations nothing uses. Prints
each change; exits non-zero on the first refused call. Reads OPENOBSERVE_URL, OPENOBSERVE_USER,
OPENOBSERVE_PASSWORD and the secrets alerts.toml names."""

import base64
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

ORG = "default"
FOLDER = "homelab"
OWNED = "homelab-"
# The value a rule's `no_data` query gives when its metric is gone.
NO_DATA = -1
# Fields the Control room reads from a rule, sent as OpenObserve's context attributes: the
# template's `{name}` placeholders.
POST = ("route", "measure", "trigger", "chart", "chart_line", "closes", "needs")

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


def seconds(duration: str | None) -> int:
    if not duration:
        return 0
    match = re.fullmatch(r"(\d+)([smh])", duration)
    if not match:
        raise SystemExit(f"{duration}: not a duration like 10m")
    return int(match[1]) * {"s": 1, "m": 60, "h": 3600}[match[2]]


def expand(text: str, env: Mapping[str, str]) -> str:
    def value(match: re.Match[str]) -> str:
        if match[1] not in env:
            raise SystemExit(f"{match[1]} is not set")
        return env[match[1]]

    return re.sub(r"\$\{(\w+)\}", value, text)


def template_name(body: str, series: list[str]) -> str:
    return OWNED + "-".join([body, *series])


def template_body(body: str, series: list[str]) -> str:
    """A body of `{placeholder}`s, each filled JSON-escaped; a series label is one on its alert."""
    if body == "discord":
        named = "".join(f" {{{label}}}" for label in series)
        line = f"**{{title}}**{named}: {{alert_status}}, {{measure}} {{alert_agg_value}}"
        return json.dumps({"content": line + "\n{text}"})
    post = {"name": "{title}", "value": "{alert_agg_value}"}
    post |= {field: f"{{{field}}}" for field in POST if field != "route"}
    return json.dumps(
        {
            "source": "openobserve",
            "status": "{alert_status}",
            "title": "{title}",
            "labels": {label: f"{{{label}}}" for label in series},
            "route": "{route}",
            "text": "{text}",
            "post": post,
            "extra": "{extra}",
        }
    )


def stream(promql: str) -> str:
    """The first metric the query reads: OpenObserve files each alert under one stream."""
    bare = re.sub(r"\{[^}]*\}|\[[^\]]*\]|\b(?:by|on|without|ignoring)\s*\([^)]*\)", " ", promql)
    names = re.findall(r"(?<![\w.])([a-zA-Z_:][a-zA-Z0-9_:]*)\b(?!\s*\()", bare)
    return next(n for n in names if n not in {"and", "or", "unless", "bool", "offset", "e"})


def desired(config: Mapping[str, Any], env: Mapping[str, str]) -> tuple[dict, dict, list[dict]]:
    """Templates, destinations and alerts by name, as OpenObserve's API takes them."""
    live = config["live"]
    templates: dict[str, dict] = {}
    destinations: dict[str, dict] = {}
    alerts = []
    for uid, rule in config["rules"].items():
        targets = config["routes"][rule["route"]] if live else ["shadow"]
        series = rule.get("series", [])
        bodies = set()
        for target in targets:
            spec = config["destinations"][target]
            bodies.add(spec["body"])
            name = OWNED + target
            templates[template_name(spec["body"], [])] = {"body": template_body(spec["body"], [])}
            headers = (
                {"Authorization": "Bearer " + expand(spec["bearer"], env)}
                if "bearer" in spec
                else {}
            )
            destinations[name] = {
                "url": expand(spec["url"], env),
                "method": "post",
                "type": "http",
                "template": template_name(spec["body"], []),
                "headers": headers,
            }
        template = None
        if series:
            # OpenObserve sends an alert's own template to all its destinations: one body.
            if len(bodies) != 1:
                raise SystemExit(f"{uid}: a per-series rule needs destinations of one body")
            body = bodies.pop()
            template = template_name(body, series)
            templates[template] = {"body": template_body(body, series)}
        query = " ".join(rule["promql"].split())
        operator, threshold = rule["operator"], rule["threshold"]
        pending = seconds(rule.get("for"))
        if keep := rule.get("keep_firing"):
            # OpenObserve's keep_firing_for never sends the resolve, so the query holds instead:
            # it fires while the value stayed past the threshold for `for` at any time in the
            # last `keep_firing`, so a dip under that is no clear but a spike is no onset.
            low, high = ("max", "min") if operator.startswith("<") else ("min", "max")
            held = f"{low}_over_time(({query})[{rule['for']}:1m])"
            query = f"{high}_over_time(({held})[{keep}:1m])"
            pending = 0
        if "no_data" in rule:
            no_data = " ".join(rule["no_data"].split())
            query = f"({query}) {operator} {threshold} or on() ({no_data}) * {NO_DATA}"
            operator, threshold = ">=", NO_DATA
        text = " ".join(" ".join([rule.get("description", ""), rule.get("action", "")]).split())
        context = {field: str(rule.get(field, "")) for field in POST}
        context |= {
            "title": rule["title"],
            "text": text,
            "extra": json.dumps(rule.get("extra", {})),
        }
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
                    "promql": query,
                    "promql_condition": {
                        "column": "value",
                        "operator": operator,
                        "value": threshold,
                    },
                    "promql_multi_alert": bool(series),
                },
                "trigger_condition": {
                    "period": 1,
                    "operator": ">=",
                    "threshold": 1,
                    "frequency": 1,
                    "frequency_type": "minutes",
                    "silence": config["repeat"].get(rule["route"], config["repeat"]["kaizen"])
                    if live
                    else 0,
                },
                "pending_period_sec": pending,
                "keep_firing_for": 0,
                # OpenObserve refuses it on a per-series alert, which never sends a resolve: the
                # Control room takes a series whose repeats stop as cleared.
                "notify_on_recovery": not series,
                "destinations": [OWNED + t for t in targets],
                "template": template,
                "context_attributes": context,
            }
        )
    return templates, destinations, alerts


def differs(want: object, have: Any) -> bool:
    """Whether `have` lacks anything in `want`. Fields OpenObserve adds are left alone, and it reads
    back an empty or false field as missing."""
    if isinstance(want, dict):
        return not isinstance(have, dict) or any(differs(v, have.get(k)) for k, v in want.items())
    if want in (None, "", False) and have in (None, "", False):
        return False
    return want != have


def apply(call: Call, config: Mapping[str, Any], env: Mapping[str, str]) -> list[str]:
    templates, destinations, alerts = desired(config, env)
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
    try:
        changes = apply(call, config, os.environ)
    except Refused as error:
        # A refusal may quote a destination's URL, and Discord's is its secret:
        # masked before the cut, so no part of it is printed.
        message = str(error)
        for spec in config["destinations"].values():
            for name in re.findall(r"\$\{(\w+)\}", " ".join(map(str, spec.values()))):
                if value := os.environ.get(name):
                    message = message.replace(value, f"${{{name}}}")
        raise SystemExit(f"OpenObserve refused {message[:500]}") from None
    print("\n".join(changes) or "OpenObserve's alerts already match", flush=True)


if __name__ == "__main__":
    main()
