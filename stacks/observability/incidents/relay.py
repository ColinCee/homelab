"""Gives each alert one post in Discord's #incidents forum and starts the cloud on-call agent on it
(docs/architecture.md, "Monitoring and alerts").

Alertmanager sends every alert here (alertmanager.yml), one alert per notification. A new alert
opens a post tagged Firing, mentioning Colin if it is `now`, and fires the agent's routine; repeats
change nothing; its end posts Cleared in the same post. The watchdog is passed on to Healthchecks.io
as a ping, so a dead relay stops the pings, and a failing one stops the watchdog (rules.yml). Only
Alertmanager reaches it, on a network of their own.
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
COLIN = "124131392207454208"
BODY_MAX = 1 << 20
DISCORD = "https://discord.com/api/v10"
# Anthropic's per-routine token starts runs only; the routine id is not a secret.
FIRE = "https://api.anthropic.com/v1/claude_code/routines/{routine}/fire"
# A Beelink outage fires about ten alerts at once; past this, posts still open without the agent.
RUNS_PER_DAY = 12
# A return this soon after clearing reopens the same post, with no new run, so a flapping alert
# takes one post and one run.
REOPEN_WITHIN = timedelta(days=1)
# A firing alert repeats at least daily (alertmanager.yml, repeat_interval); one unseen for longer
# was held back by an inhibit rule, which never sends its end, so its post is forgotten.
FORGET_AFTER = timedelta(days=3)
# Label values reach the agent's prompt. These rules' labels carry text from outside the Beelink
# (rules.yml: ssh-login-elsewhere, ci-host-install), so theirs never do.
OUTSIDE = frozenset({"user", "from", "device", "author", "commit"})
PLAIN = re.compile(r"[\w.:/@-]{0,64}")

Discord = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]
Fire = Callable[[str], str]
Ping = Callable[[], None]
FAILED = (OSError, KeyError, ValueError, TypeError, AttributeError)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class Relay:
    def __init__(
        self, state_file: Path, forum: str, guild: str, discord: Discord, fire: Fire, ping: Ping
    ) -> None:
        self.state_file = state_file
        self.forum = forum
        self.guild = guild
        self.discord = discord
        self.fire = fire
        self.ping = ping
        self._tags: dict[str, str] | None = None
        self.state = self._load()

    def handle(self, payload: object) -> int:
        alerts = payload.get("alerts") if isinstance(payload, dict) else None
        if not isinstance(alerts, list) or not alerts or not all(_is_alert(a) for a in alerts):
            return 400
        for alert in alerts:
            if alert["labels"].get("alertname") == "watchdog":
                self._watchdog(alert)
                continue
            done = self._open(alert) if alert["status"] == "firing" else self._clear(alert)
            if not done:
                # Alertmanager sends it again; what was saved lets the retry finish the job.
                return 502
        return 200

    def _watchdog(self, alert: dict[str, Any]) -> None:
        if alert["status"] != "firing":
            return
        try:
            self.ping()
        except FAILED as e:
            # Never a 502: Alertmanager would count the relay as failing and stop the watchdog
            # for an hour, which Healthchecks.io would page as alerting down.
            log(f"cannot ping Healthchecks.io: {e}")

    def _open(self, alert: dict[str, Any]) -> bool:
        post = self.state["posts"].get(alert["fingerprint"])
        if post and not post["cleared"]:
            post["seen"] = _now().isoformat()
            self._save()
            return True
        if post and _now() - datetime.fromisoformat(post["cleared"]) < REOPEN_WITHIN:
            if not self._retag(post["thread"], "Firing", "Firing again.", _mentions(alert)):
                return False
            post.update(seen=_now().isoformat(), cleared=None)
            self._save()
            return True
        try:
            thread = self.discord(
                "POST",
                f"/channels/{self.forum}/threads",
                {
                    "name": _title(alert)[:100],
                    "message": {"content": _post(alert), "allowed_mentions": _mentions(alert)},
                    **self._tags_for("Firing"),
                },
            )["id"]
        except FAILED as e:
            log(f"cannot open a post for {alert['labels'].get('alertname')}: {e}")
            return False
        self.state["posts"][alert["fingerprint"]] = {
            "thread": thread,
            "seen": _now().isoformat(),
            "cleared": None,
        }
        self._save()
        self._say(thread, self._start_agent(alert, thread))
        return True

    def _clear(self, alert: dict[str, Any]) -> bool:
        post = self.state["posts"].get(alert["fingerprint"])
        if post is None or post["cleared"]:
            return True
        text = "Cleared: the number is back within bounds; that does not mean the cause is fixed."
        if not self._retag(post["thread"], "Cleared", text):
            return False
        post["cleared"] = _now().isoformat()
        self._save()
        return True

    def _retag(
        self, thread: str, tag: str, text: str, mentions: dict[str, list[str]] | None = None
    ) -> bool:
        try:
            # Discord archives an idle post, and an archived one takes no message or tag change.
            self.discord("PATCH", f"/channels/{thread}", {"archived": False, **self._tags_for(tag)})
            self.discord(
                "POST",
                f"/channels/{thread}/messages",
                {
                    "content": _mention(mentions) + text,
                    "allowed_mentions": mentions or {"parse": []},
                },
            )
        except FAILED as e:
            log(f"cannot mark post {thread} {tag}: {e}")
            return False
        return True

    def _start_agent(self, alert: dict[str, Any], thread: str) -> str:
        runs = self.state["runs"]
        today = _now().date().isoformat()
        if runs["day"] != today:
            runs.update(day=today, count=0)
        if runs["count"] >= RUNS_PER_DAY:
            return f"On-call agent not started: {RUNS_PER_DAY} runs today already."
        try:
            url = self.fire(_snapshot(alert, f"https://discord.com/channels/{self.guild}/{thread}"))
        except FAILED as e:
            # No retry: a fire has no idempotency key, so a retry could start two runs.
            log(f"cannot start the on-call agent: {e}")
            return "On-call agent not started: the relay could not reach its routine (see its log)."
        runs["count"] += 1
        self._save()
        return f"On-call agent: {url}"

    def _say(self, thread: str, text: str) -> None:
        try:
            self.discord(
                "POST",
                f"/channels/{thread}/messages",
                {"content": text, "allowed_mentions": {"parse": []}},
            )
        except FAILED as e:
            log(f"cannot post in {thread}: {e}")

    def _tags_for(self, name: str) -> dict[str, list[str]]:
        """The tag to set, or none when the forum's tags cannot be read: never an empty list,
        which would clear the post's tags."""
        if self._tags is None:
            try:
                forum = self.discord("GET", f"/channels/{self.forum}", None)
                self._tags = {t["name"]: t["id"] for t in forum.get("available_tags", [])}
            except FAILED as e:
                log(f"cannot read the forum's tags: {e}")
                return {}
        if name not in self._tags:
            log(f"the forum has no {name} tag")
            self._tags = None
            return {}
        return {"applied_tags": [self._tags[name]]}

    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except OSError, ValueError:
            state = {}
        state.setdefault("posts", {})
        state.setdefault("runs", {"day": "", "count": 0})
        return state

    def _save(self) -> None:
        now = _now()
        self.state["posts"] = {
            k: v
            for k, v in self.state["posts"].items()
            if (
                now - datetime.fromisoformat(v["cleared"]) < REOPEN_WITHIN
                if v["cleared"]
                else now - datetime.fromisoformat(v["seen"]) < FORGET_AFTER
            )
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
    return {
        k: v if k not in OUTSIDE and PLAIN.fullmatch(str(v)) else "(withheld)"
        for k, v in alert["labels"].items()
    }


def _mentions(alert: dict[str, Any]) -> dict[str, list[str]]:
    """Colin, while an alert that needs him now fires; no one else, whatever the text says."""
    users = [COLIN] if alert["labels"].get("level") == "now" else []
    return {"parse": [], "users": users}


def _mention(mentions: dict[str, list[str]] | None) -> str:
    return "".join(f"<@{u}> " for u in (mentions or {}).get("users", []))


def _title(alert: dict[str, Any]) -> str:
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
    # The mention first, so the cut never drops it.
    return (_mention(_mentions(alert)) + "\n".join(lines))[:2000]


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
    # Two tries at most: Alertmanager stops waiting after about a minute, and retries itself.
    for _ in range(2):
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
            time.sleep(min(float(e.headers.get("Retry-After") or 1), 5))
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
        "User-Agent": "homelab-incidents/1",
    }
    routine = os.environ.get("ONCALL_ROUTINE", "")
    healthchecks = _secret("healthchecks_ping")
    fire_url = FIRE.format(routine=routine)

    def discord(method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        return _request(DISCORD + path, method, bot, body)

    def fire(text: str) -> str:
        if not routine:
            raise ValueError("ONCALL_ROUTINE is not set")
        return str(
            _request(fire_url, "POST", fire_headers, {"text": text})["claude_code_session_url"]
        )

    def ping() -> None:
        req = urllib.request.Request(
            healthchecks, method="POST", headers={"User-Agent": "homelab-incidents/1"}
        )
        with urllib.request.urlopen(req, timeout=10):
            pass

    relay = Relay(
        Path("/state/state.json"),
        os.environ["INCIDENTS_FORUM"],
        os.environ["DISCORD_GUILD"],
        discord,
        fire,
        ping,
    )

    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def do_POST(self) -> None:
            length = self.headers.get("Content-Length") or "0"
            raw = self.rfile.read(min(int(length), BODY_MAX)) if length.isdigit() else b""
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
