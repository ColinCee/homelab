"""The on-call bot (stacks/oncall/bot.py) opens one incident per firing alert, notices it clear,
reads the owner's replies right, and keeps the agent's pull requests off main."""

from pathlib import Path
from typing import Any

import pytest

from stacks.oncall import bot


def alert(fingerprint: str, name: str = "high-disk", **labels: str) -> dict[str, Any]:
    return {
        "fingerprint": fingerprint,
        "labels": {"alertname": name, "level": "week", **labels},
        "annotations": {
            "title": "Disk filling",
            "measure": "Used",
            "value": "91",
            "unit": "%",
            "harm": "Writes fail when it is full.",
            "action": "Prune old images.",
            "dashboard": "http://grafana.internal/d/home",
        },
    }


def test_an_alert_opens_one_incident_and_its_clear_and_return_are_each_noticed_once() -> None:
    state = bot.State()
    new, again, cleared = bot.reconcile(state, [alert("a"), alert("w", "watchdog")])
    assert [a["fingerprint"] for a in new] == ["a"] and again == cleared == []

    state.incidents["s1"] = bot.Incident(session_id="s1", fingerprint="a", firing=True)
    assert bot.reconcile(state, [alert("a")]) == ([], [], [])

    _, _, cleared = bot.reconcile(state, [])
    assert [i.session_id for i in cleared] == ["s1"]
    cleared[0].firing = False
    assert bot.reconcile(state, []) == ([], [], [])

    # Back before the incident closed (an Alertmanager restart, a flap): the same post.
    new, again, _ = bot.reconcile(state, [alert("a")])
    assert new == [] and [i.session_id for i in again] == ["s1"]


def test_a_muted_alert_is_not_posted_again_while_it_fires() -> None:
    state = bot.State()
    state.incidents["s1"] = bot.Incident(session_id="s1", fingerprint="a", firing=True, muted=True)
    assert bot.reconcile(state, [alert("a")]) == ([], [], [])


def test_an_alert_that_fires_after_its_incident_closed_opens_a_new_one() -> None:
    state = bot.State()
    state.incidents["s1"] = bot.Incident(session_id="s1", fingerprint="a", closed=True)
    new, _, _ = bot.reconcile(state, [alert("a")])
    assert [a["fingerprint"] for a in new] == ["a"]


def test_the_state_survives_a_restart_and_forgets_long_closed_incidents(tmp_path: Path) -> None:
    state = bot.State(last_sweep=5.0)
    state.incidents["s1"] = bot.Incident(session_id="s1", thread_id=9, seen=["e1"])
    state.incidents["old"] = bot.Incident(session_id="old", closed=True, closed_at=1.0)
    path = tmp_path / "state.json"
    state.save(path, now=1.0 + bot.KEEP_CLOSED + 1)
    loaded = bot.State.load(path)
    assert loaded == state and list(loaded.incidents) == ["s1"]
    assert loaded.by_thread(9) == state.incidents["s1"]


def test_a_post_shows_the_rule_and_mentions_the_owner_only_for_an_act_now_alert() -> None:
    week = alert("a", name="high-disk", name_label="x")
    assert bot.alert_title(week) == "Disk filling · x"
    post = bot.alert_post(week, 42)
    assert "Used: **91%**" in post and "**What to do:** Prune old images." in post
    assert "<@42>" not in post
    assert "<@42>" in bot.alert_post(alert("b", level="now"), 42)


@pytest.mark.parametrize(
    ("value", "shown"), [("-1", "no data"), ("-1.0", "no data"), ("-10", "-10%")]
)
def test_only_minus_one_means_the_metric_is_gone(value: str, shown: str) -> None:
    gone = alert("a")
    gone["annotations"]["value"] = value
    assert f"**{shown}**" in bot.alert_post(gone, 42)


@pytest.mark.parametrize(
    ("text", "pending", "intent"),
    [
        ("Stop", None, "interrupt"),
        ("stop.", "sevt_1", "interrupt"),
        ("yes", "sevt_1", "allow"),
        ("Go ahead!", "sevt_1", "allow"),
        ("no, check the logs first", "sevt_1", "deny"),
        ("yes", None, "message"),
        ("look at the disk", None, "message"),
    ],
)
def test_the_owners_reply_means_what_it_says(text: str, pending: str | None, intent: str) -> None:
    assert bot.owner_intent(text, pending) == intent


@pytest.mark.parametrize("name", ["main", "../main", "refs/heads/main", "Fix Disk Rule!", ""])
def test_every_branch_is_under_oncall(name: str) -> None:
    branch = bot.pr_branch(name, 1700000000)
    assert branch.startswith("oncall/") and branch.endswith("-1700000000")
    assert ".." not in branch and branch.count("/") == 1


@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "a/../../b", ""])
def test_a_file_outside_the_repository_is_refused(path: str) -> None:
    with pytest.raises(ValueError, match="not a repository path"):
        bot.check_path(path)


def test_a_long_message_splits_under_discords_limit_at_line_ends() -> None:
    text = "\n".join(f"line {i}" * 20 for i in range(100))
    parts = bot.chunks(text)
    assert all(len(p) <= bot.DISCORD_LIMIT for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def test_a_secret_never_reaches_discord_or_a_pull_request() -> None:
    secrets = ["hunter2", "https://hc-ping.com/abc"]
    assert bot.redact("password hunter2 and https://hc-ping.com/abc", secrets) == (
        "password [redacted] and [redacted]"
    )
    assert bot.holds_secret("a log line: hunter2", secrets)
    assert not bot.holds_secret("nothing here", secrets)
