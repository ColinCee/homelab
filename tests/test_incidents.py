"""The incidents relay (stacks/observability/incidents/relay.py) opens one post per alert,
starts the on-call agent once, clears the same post and survives a restart; Alertmanager sends
it every alert but the watchdog and still posts each to #alerts."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from stacks.observability.incidents import relay

ALERTMANAGER = yaml.safe_load(
    (Path(__file__).parent.parent / "stacks/observability/alertmanager/alertmanager.yml").read_text(
        encoding="utf-8"
    )
)
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


@pytest.fixture
def parts(tmp_path: Path) -> tuple[relay.Relay, FakeDiscord, FakeFire]:
    discord, fire = FakeDiscord(), FakeFire()
    return relay.Relay(tmp_path / "state.json", "forum", "guild", discord, fire), discord, fire


def test_a_new_alert_opens_one_post_and_starts_the_agent_once(parts: Parts) -> None:
    r, discord, fire = parts
    assert r.handle(alert()) == 200
    assert r.handle(alert()) == 200
    assert discord.threads == 1 and len(fire.texts) == 1
    opened = next(b for m, p, b in discord.calls if p == "/channels/forum/threads")
    assert opened is not None
    assert opened["name"] == "Disk filling" and opened["applied_tags"] == ["t-firing"]
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
    assert discord.said("thread-1")[-1] == "Firing again."


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
    again = relay.Relay(tmp_path / "state.json", "forum", "guild", discord, fire)
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


def receivers(route: dict[str, Any], labels: dict[str, str]) -> list[str]:
    """Alertmanager's routing for the `=` and `!=` matchers alertmanager.yml uses: the first
    matching child and any after it while each continues; the route's own receiver if none."""
    found: list[str] = []
    for child in route.get("routes", []):
        if all(_matches(m, labels) for m in child.get("matchers", [])):
            found += receivers({"receiver": route["receiver"], **child}, labels)
            if not child.get("continue"):
                return found
    return found or [route["receiver"]]


def _matches(matcher: str, labels: dict[str, str]) -> bool:
    found = re.fullmatch(r'(\w+)(!?=)"(.*)"', matcher)
    assert found, matcher
    name, op, value = found.groups()
    return (labels.get(name) == value) == (op == "=")


@pytest.mark.parametrize("level", ["now", "week"])
def test_every_alert_but_the_watchdog_reaches_the_relay_and_still_posts_to_alerts(
    level: str,
) -> None:
    labels = {"alertname": "disk-filling", "level": level}
    assert receivers(ALERTMANAGER["route"], labels) == ["incidents", "discord"]
    assert receivers(ALERTMANAGER["route"], {"alertname": "watchdog"}) == ["healthchecks"]
    receiver = next(r for r in ALERTMANAGER["receivers"] if r["name"] == "incidents")
    assert receiver["webhook_configs"][0]["send_resolved"] is True
