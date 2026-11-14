"""The incidents relay (stacks/observability/incidents/relay.py) opens one post per alert,
mentions Colin only for a firing `now` alert, starts the on-call agent once, clears the same post,
survives a restart and passes the watchdog on to Healthchecks.io."""

import json
import re
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from stacks.observability.incidents import relay

TAGS = {
    "available_tags": [{"name": "Firing", "id": "t-firing"}, {"name": "Cleared", "id": "t-cleared"}]
}


class FakeDiscord:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.threads = 0
        self.fail_open = False
        self.fail_patch = False

    def __call__(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        self.calls.append((method, path, body))
        if method == "GET":
            return TAGS
        if method == "PATCH" and self.fail_patch:
            raise OSError("discord down")
        if path.endswith("/threads"):
            if self.fail_open:
                raise OSError("discord down")
            self.threads += 1
            return {"id": f"thread-{self.threads}"}
        return {}

    def said(self, thread: str) -> list[str]:
        return [b["content"] for m, p, b in self.calls if p == f"/channels/{thread}/messages" and b]


class FakeFire:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.fail = False

    def __call__(self, text: str) -> str:
        if self.fail:
            raise OSError("refused")
        self.texts.append(text)
        return f"https://claude.ai/code/session_{len(self.texts)}"


class FakePing:
    def __init__(self) -> None:
        self.count = 0
        self.fail = False

    def __call__(self) -> None:
        if self.fail:
            raise OSError("healthchecks down")
        self.count += 1


def ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def alert(status: str = "firing", fingerprint: str = "fp1", **labels: str) -> dict[str, Any]:
    return {
        "alerts": [
            {
                "status": status,
                "fingerprint": fingerprint,
                "labels": {"alertname": "disk-filling", "level": "now", **labels},
                "annotations": {
                    "title": "Disk filling",
                    "measure": "Full in",
                    "value": "5",
                    "unit": " h",
                },
                "startsAt": "2026-11-13T01:00:00Z",
            }
        ]
    }


Parts = tuple[relay.Relay, "FakeDiscord", "FakeFire"]


def FIRST_LINE(query: str) -> str:
    """The log store, for the alerts that read none."""
    return ""


@pytest.fixture
def ping() -> FakePing:
    return FakePing()


@pytest.fixture
def parts(tmp_path: Path, ping: FakePing) -> tuple[relay.Relay, FakeDiscord, FakeFire]:
    discord, fire = FakeDiscord(), FakeFire()
    r = relay.Relay(tmp_path / "state.json", "forum", "guild", discord, fire, ping, FIRST_LINE)
    return r, discord, fire


def opened(discord: FakeDiscord) -> dict[str, Any]:
    return next(b for m, p, b in discord.calls if p.endswith("/threads") and b)


def test_a_new_alert_opens_one_post_and_starts_the_agent_once(parts: Parts) -> None:
    r, discord, fire = parts
    assert r.handle(alert()) == 200
    assert r.handle(alert()) == 200
    assert discord.threads == 1 and len(fire.texts) == 1
    post = opened(discord)
    assert post["name"] == "Disk filling" and post["applied_tags"] == ["t-firing"]
    assert discord.said("thread-1") == ["On-call agent: https://claude.ai/code/session_1"]


def test_its_end_clears_the_same_post_and_a_quick_return_reopens_it(parts: Parts) -> None:
    r, discord, fire = parts
    r.handle(alert())
    assert r.handle(alert("resolved")) == 200
    assert discord.said("thread-1")[-1].startswith("Cleared")
    patch = ("PATCH", "/channels/thread-1", {"archived": False, "applied_tags": ["t-cleared"]})
    assert patch in discord.calls
    r.handle(alert())
    assert discord.threads == 1 and len(fire.texts) == 1
    assert discord.said("thread-1")[-1].endswith("Firing again.")


def test_a_return_a_day_after_clearing_is_a_new_incident(parts: Parts) -> None:
    r, discord, fire = parts
    r.handle(alert())
    r.handle(alert("resolved"))
    r.state["posts"]["fp1"]["cleared"] = ago(days=2)
    r.handle(alert())
    assert discord.threads == 2 and len(fire.texts) == 2


def test_a_failed_clear_is_sent_again(parts: Parts) -> None:
    r, discord, _ = parts
    r.handle(alert())
    discord.fail_patch = True
    assert r.handle(alert("resolved")) == 502
    discord.fail_patch = False
    assert r.handle(alert("resolved")) == 200
    assert discord.said("thread-1")[-1].startswith("Cleared")


def test_a_restart_opens_nothing_again(parts: Parts, tmp_path: Path) -> None:
    r, discord, fire = parts
    r.handle(alert())
    again = relay.Relay(
        tmp_path / "state.json", "forum", "guild", discord, fire, FakePing(), FIRST_LINE
    )
    again.handle(alert())
    assert discord.threads == 1 and len(fire.texts) == 1


def test_a_failed_post_asks_alertmanager_to_send_it_again(parts: Parts) -> None:
    r, discord, fire = parts
    discord.fail_open = True
    assert r.handle(alert()) == 502
    discord.fail_open = False
    assert r.handle(alert()) == 200
    assert discord.threads == 1 and len(fire.texts) == 1


def test_a_refused_fire_is_said_in_the_post_and_not_retried(parts: Parts) -> None:
    r, discord, fire = parts
    fire.fail = True
    assert r.handle(alert()) == 200
    assert discord.said("thread-1") == [
        "On-call agent not started: the relay could not reach its routine (see its log)."
    ]


def test_runs_stop_at_the_daily_cap_but_posts_still_open(parts: Parts) -> None:
    r, discord, fire = parts
    for i in range(relay.RUNS_PER_DAY + 2):
        r.handle(alert(fingerprint=f"fp{i}"))
    assert discord.threads == relay.RUNS_PER_DAY + 2
    assert len(fire.texts) == relay.RUNS_PER_DAY
    assert "not started" in discord.said(f"thread-{relay.RUNS_PER_DAY + 2}")[0]


def test_labels_from_outside_never_reach_the_agent(parts: Parts) -> None:
    r, _, fire = parts
    r.handle(alert(name="backup", author="merge-pr-1", job="say hello"))
    assert "merge-pr" not in fire.texts[0] and "say hello" not in fire.texts[0]
    assert '"name": "backup"' in fire.texts[0]


def test_a_long_firing_alert_keeps_its_post_and_an_unseen_one_is_forgotten(parts: Parts) -> None:
    r, discord, fire = parts
    r.handle(alert())
    r.handle(alert(fingerprint="held"))
    r.state["posts"]["fp1"]["seen"] = ago(days=2)
    r.state["posts"]["held"]["seen"] = ago(days=4)
    r.handle(alert())
    assert "fp1" in r.state["posts"] and "held" not in r.state["posts"]
    assert discord.threads == 2 and len(fire.texts) == 2


@pytest.mark.parametrize("payload", [{}, {"alerts": []}, {"alerts": [{"status": "firing"}]}, []])
def test_anything_but_an_alertmanager_alert_is_refused(parts: Parts, payload: object) -> None:
    r, discord, _ = parts
    assert r.handle(payload) == 400
    assert not discord.calls


def test_an_act_now_alert_mentions_colin_when_it_opens_and_returns_only(parts: Parts) -> None:
    r, discord, _ = parts
    r.handle(alert())
    message = opened(discord)["message"]
    assert message["content"].startswith(f"<@{relay.COLIN}> ")
    assert message["allowed_mentions"] == {"parse": [], "users": [relay.COLIN]}
    r.handle(alert())
    r.handle(alert("resolved"))
    assert not any("<@" in t for t in discord.said("thread-1"))
    r.handle(alert())
    assert discord.said("thread-1")[-1] == f"<@{relay.COLIN}> Firing again."


def test_a_this_week_alert_mentions_no_one(parts: Parts) -> None:
    r, discord, _ = parts
    r.handle(alert(level="week"))
    message = opened(discord)["message"]
    assert "<@" not in message["content"]
    assert message["allowed_mentions"] == {"parse": [], "users": []}


def test_the_watchdog_pings_healthchecks_and_never_posts(parts: Parts, ping: FakePing) -> None:
    r, discord, fire = parts
    assert r.handle(alert(alertname="watchdog", level="watchdog")) == 200
    assert r.handle(alert("resolved", alertname="watchdog", level="watchdog")) == 200
    assert ping.count == 1 and not discord.calls and not fire.texts


def test_a_failed_ping_is_not_a_relay_failure(parts: Parts, ping: FakePing) -> None:
    # A 502 would stop the watchdog for an hour (rules.yml), paging Colin over a blip.
    r, _, _ = parts
    ping.fail = True
    assert r.handle(alert(alertname="watchdog", level="watchdog")) == 200


def errors_alert(service: str) -> dict[str, Any]:
    errors = alert(alertname="errors-logged", level="week", service_name=service)
    errors["alerts"][0]["annotations"]["logs"] = f'service_name:"{service}" level:in(error,fatal)'
    return errors


def test_an_error_alert_shows_its_first_line_and_links_its_lines(tmp_path: Path) -> None:
    discord, fire, asked = FakeDiscord(), FakeFire(), []

    def first_line(query: str) -> str:
        asked.append(query)
        return "panic at ``` <@124> [x](http://evil)"

    r = relay.Relay(tmp_path / "s.json", "forum", "guild", discord, fire, FakePing(), first_line)
    assert r.handle(errors_alert("proxy")) == 200
    # From 5 minutes before it started: its line came at most a scrape and a rule run earlier.
    assert asked == ['_time:[2026-11-13T00:55:00Z, now] service_name:"proxy" level:in(error,fatal)']
    post = opened(discord)["message"]
    # The line stays in its code block, so it can mention or link no one.
    assert "```\npanic at ''' <@124> [x](http://evil)\n```" in post["content"]
    assert post["allowed_mentions"] == {"parse": [], "users": []}
    link = re.search(r"\[Its lines\]\((\S+)\)", post["content"])
    assert link and link.group(1).startswith("http://grafana.internal/explore?")
    pane = json.loads(urllib.parse.unquote(link.group(1).split("panes=")[1]))
    assert pane["a"]["queries"][0]["expr"] == 'service_name:"proxy" level:in(error,fatal)'
    # The agent gets the query, never the line, whose text comes from outside.
    assert "panic" not in fire.texts[0] and "level:in(error,fatal)" in fire.texts[0]


def test_an_error_alert_still_posts_when_the_log_store_does_not_answer(tmp_path: Path) -> None:
    def first_line(query: str) -> str:
        raise OSError("gateway down")

    discord = FakeDiscord()
    r = relay.Relay(tmp_path / "s.json", "f", "g", discord, FakeFire(), FakePing(), first_line)
    assert r.handle(errors_alert("x")) == 200
    assert "did not answer" in opened(discord)["message"]["content"]
