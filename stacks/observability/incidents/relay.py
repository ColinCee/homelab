"""Gives each alert one post in Discord's #incidents forum and starts the cloud on-call agent on it
(docs/architecture.md, "Monitoring and alerts").

Alertmanager posts every firing alert here (alertmanager.yml, receiver `incidents`), one alert per
notification. A new alert opens a post tagged Firing and fires the agent's routine; repeats change
nothing; its end posts Cleared in the same post. Only Alertmanager reaches it, on a network of
their own.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

PORT = 8080
BODY_MAX = 1 << 20
DISCORD = "https://discord.com/api/v10"
# Anthropic's per-routine token starts runs only; the routine id is not a secret.
FIRE = "https://api.anthropic.com/v1/claude_code/routines/{routine}/fire"
# A Beelink outage fires about ten alerts at once; past this, posts still open without the agent.
RUNS_PER_DAY = 12
# An alert held back by an inhibit rule never sends its end, so its post would stay open for ever.
OPEN_FOR = timedelta(days=14)
# Label values reach the agent's prompt; the SSH rule's carry text from outside the Beelink.
PLAIN = re.compile(r"^[\w.:/@-]{0,64}$")

Discord = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]
Fire = Callable[[str], str]


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class Relay:
    def __init__(
        self, state_file: Path, forum: str, guild: str, discord: Discord, fire: Fire
    ) -> None:
        self.state_file = state_file
        self.forum = forum
        self.guild = guild
        self.discord = discord
        self.fire = fire
        self._tags: dict[str, str] | None = None
        self.state = self._load()

    def handle(self, payload: object) -> int:
        alerts = payload.get("alerts") if isinstance(payload, dict) else None
        if not isinstance(alerts, list) or not alerts or not all(_is_alert(a) for a in alerts):
            return 400
        for alert in alerts:
            if alert["status"] == "firing":
                if not self._open(alert):
                    return 502
            else:
                self._clear(alert)
        return 200

    def _open(self, alert: dict[str, Any]) -> bool:
        fingerprint = alert["fingerprint"]
        if fingerprint in self.state["open"]:
            return True
        try:
            thread = self.discord(
                "POST",
                f"/channels/{self.forum}/threads",
                {
                    "name": _title(alert)[:100],
                    "message": {"content": _post(alert), "allowed_mentions": {"parse": []}},
                    "applied_tags": self._tag("Firing"),
                },
            )["id"]
        except (OSError, KeyError, ValueError) as e:
            # Alertmanager sends it again, and nothing was saved, so the retry opens it.
            log(f"cannot open a post for {alert['labels'].get('alertname')}: {e}")
            return False
        self.state["open"][fingerprint] = {"thread": thread, "opened": _now().isoformat()}
        self._save()
        self._say(thread, self._start_agent(alert, thread))
        return True

    def _clear(self, alert: dict[str, Any]) -> None:
        entry = self.state["open"].pop(alert["fingerprint"], None)
        if entry is None:
            return
        self._save()
        thread = entry["thread"]
        self._say(
            thread,
            "Cleared: the number is back within bounds; that does not mean the cause is fixed.",
        )
        try:
            # Discord archives an idle post, and an archived one takes no tag change.
            self.discord(
                "PATCH",
                f"/channels/{thread}",
                {"archived": False, "applied_tags": self._tag("Cleared")},
            )
        except (OSError, KeyError, ValueError) as e:
            log(f"cannot tag post {thread} Cleared: {e}")

    def _start_agent(self, alert: dict[str, Any], thread: str) -> str:
        runs = self.state["runs"]
        today = _now().date().isoformat()
        if runs["day"] != today:
            runs.update(day=today, count=0)
        if runs["count"] >= RUNS_PER_DAY:
            return f"On-call agent not started: {RUNS_PER_DAY} runs today already."
        runs["count"] += 1
        self._save()
        try:
            url = self.fire(_snapshot(alert, f"https://discord.com/channels/{self.guild}/{thread}"))
        except (OSError, KeyError, ValueError) as e:
            # No retry: a fire has no idempotency key, so a retry could start two runs.
            log(f"cannot start the on-call agent: {e}")
            return "On-call agent not started: the relay could not reach its routine (see its log)."
        return f"On-call agent: {url}"

    def _say(self, thread: str, text: str) -> None:
        try:
            self.discord(
                "POST",
                f"/channels/{thread}/messages",
                {"content": text, "allowed_mentions": {"parse": []}},
            )
        except (OSError, KeyError, ValueError) as e:
            log(f"cannot post in {thread}: {e}")

    def _tag(self, name: str) -> list[str]:
        if self._tags is None:
            try:
                forum = self.discord("GET", f"/channels/{self.forum}", None)
                self._tags = {t["name"]: t["id"] for t in forum.get("available_tags", [])}
            except (OSError, KeyError, ValueError) as e:
                log(f"cannot read the forum's tags: {e}")
                return []
        return [self._tags[name]] if name in self._tags else []

    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except OSError, ValueError:
            state = {}
        state.setdefault("open", {})
        state.setdefault("runs", {"day": "", "count": 0})
        return state

    def _save(self) -> None:
        cutoff = _now() - OPEN_FOR
        self.state["open"] = {
            k: v
            for k, v in self.state["open"].items()
            if datetime.fromisoformat(v["opened"]) > cutoff
        }
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state), encoding="utf-8")
        tmp.replace(self.state_file)


def _is_alert(alert: object) -> bool:
    return (
        isinstance(alert, dict)
        and alert.get("status") in ("firing", "resolved")
        and isinstance(alert.get("fingerprint"), str)
        and isinstance(alert.get("labels"), dict)
        and isinstance(alert.get("annotations"), dict)
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _labels(alert: dict[str, Any]) -> dict[str, str]:
    return {k: v if PLAIN.match(str(v)) else "(withheld)" for k, v in alert["labels"].items()}


def _title(alert: dict[str, Any]) -> str:
    # As the #alerts post's title (../alertmanager/discord.tmpl, homelab.title).
    labels = _labels(alert)
    extra = [v for k, v in sorted(labels.items()) if k not in ("alertname", "level")]
    return " · ".join(
        [alert["annotations"].get("title") or labels.get("alertname", "alert"), *extra]
    )


def _post(alert: dict[str, Any]) -> str:
    a = alert["annotations"]
    value = a.get("value", "")
    shown = "no data" if re.fullmatch(r"-1(\.0*)?", value) else f"{value}{a.get('unit', '')}"
    lines = [
        f"{a.get('measure', 'Value')}: **{shown}**",
        f"**Why it matters:** {a.get('harm', '')}",
        f"**What to do:** {a.get('action', '')}",
    ]
    if a.get("dashboard"):
        lines.append(f"[Dashboards]({a['dashboard']})")
    return "\n".join(lines)[:2000]


def _snapshot(alert: dict[str, Any], post: str) -> str:
    keep = ("title", "measure", "value", "unit", "harm", "action", "dashboard")
    return json.dumps(
        {
            "labels": _labels(alert),
            "annotations": {k: v for k, v in alert["annotations"].items() if k in keep},
            "startsAt": alert.get("startsAt"),
            "post": post,
        },
        indent=1,
    )


def _request(
    url: str, method: str, headers: dict[str, str], body: dict[str, Any] | None
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode()
    for _ in range(3):
        req = urllib.request.Request(
            url, data=data, method=method, headers={**headers, "Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(min(float(json.loads(e.read() or b"{}").get("retry_after", 1)), 5))
    raise OSError(f"{method} {url}: still rate limited")


def _secret(name: str) -> str:
    return Path(f"/run/secrets/{name}").read_text(encoding="utf-8").strip()


def main() -> None:
    bot = {
        "Authorization": f"Bot {_secret('incidents_discord_bot_token')}",
        "User-Agent": "DiscordBot (homelab, 1)",
    }
    fire_headers = {
        "Authorization": f"Bearer {_secret('oncall_fire_token')}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "experimental-cc-routine-2026-04-01",
    }
    routine = os.environ.get("ONCALL_ROUTINE", "")
    fire_url = FIRE.format(routine=routine)

    def discord(method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        return _request(DISCORD + path, method, bot, body)

    def fire(text: str) -> str:
        if not routine:
            raise ValueError("ONCALL_ROUTINE is not set")
        return str(
            _request(fire_url, "POST", fire_headers, {"text": text})["claude_code_session_url"]
        )

    relay = Relay(
        Path("/state/state.json"),
        os.environ["INCIDENTS_FORUM"],
        os.environ["DISCORD_GUILD"],
        discord,
        fire,
    )

    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def do_POST(self) -> None:
            raw = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), BODY_MAX))
            if self.path != "/alert":
                code = 404
            else:
                try:
                    code = relay.handle(json.loads(raw))
                except ValueError:
                    code = 400
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    # One request at a time: Alertmanager's retry of a slow post waits, then finds it already open.
    HTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
