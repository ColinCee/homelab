"""The incidents relay's promises (relay.py; docs/architecture.md, "Incidents"), held over any
run of Alertmanager notifications for one rule: series joining, leaving and repeating, Discord
blips and Alertmanager's retries, Colin deleting the post, agent reports, restarts and time
passing. Each promise was a bug fixed by hand once: #1495, #1497, #1528, #1532."""

import email.message
import re
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from stacks.observability.incidents import relay

TAGS = {
    "available_tags": [{"name": "Firing", "id": "firing"}, {"name": "Cleared", "id": "cleared"}]
}
SERIES = [f"fp{n}" for n in range(4)]
REPORTS = ["Needs you: nothing", "Needs you: restart it", "**Needs you:** None."]
# Gaps between notifications: Alertmanager repeats a firing rule at least daily, so a rule that
# keeps returning within the reopen window is one incident.
GAPS = st.timedeltas(min_value=timedelta(0), max_value=relay.REOPEN_WITHIN - timedelta(minutes=1))


class Forum:
    """Discord's #incidents as the relay sees it: each post's tag, whether it is archived, and its
    messages; a deleted post answers 404, and FAILS of the next calls about a post drop."""

    def __init__(self) -> None:
        self.posts: dict[str, dict[str, Any]] = {}
        self.deleted: set[str] = set()
        self.fails = 0

    def __call__(self, method: str, path: str, body: dict[str, Any] | None) -> Any:  # noqa: ANN401
        if path == "/channels/forum":
            return TAGS
        if self.fails:
            self.fails -= 1
            raise OSError("discord dropped the call")
        if path == "/channels/forum/threads":
            assert body
            thread = f"thread-{len(self.posts) + 1}"
            self.posts[thread] = {"archived": False, "tags": body.get("applied_tags"), "said": []}
            self.posts[thread]["said"].append({"id": thread, **body["message"]})
            return {"id": thread}
        found = re.fullmatch(r"/channels/([\w-]+)(/messages)?(\?limit=(\d+))?", path)
        assert found, path
        thread, messages, _, limit = found.groups()
        if thread in self.deleted:
            raise urllib.error.HTTPError(path, 404, "Unknown", email.message.Message(), None)
        post = self.posts[thread]
        if method == "GET":
            return post["said"][::-1][: int(limit)]
        assert body is not None
        if messages:
            post["said"].append({"id": f"m{len(post['said'])}", "content": body["content"]})
        else:
            post["archived"] = body["archived"]
            post["tags"] = body.get("applied_tags", post["tags"])
        return {}

    def report(self, thread: str) -> str | None:
        """The step the agent's newest report in sight names, as homelab-oncall writes it."""
        return relay._report(self.posts[thread]["said"][::-1][: relay.REPORT_WITHIN], thread)


class Relay(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.dir = TemporaryDirectory()
        self.now = datetime(2026, 11, 17, tzinfo=UTC)
        self.patch = pytest.MonkeyPatch()
        self.patch.setattr(relay, "_now", lambda: self.now)
        self.forum = Forum()
        self.fired: list[str] = []
        self.fire_fails = False
        self.relay = self._start()
        # The series the rule's last delivered notification said were firing.
        self.firing: list[str] = []
        self.deletes = 0
        # Colin deleted the post while its alert fired: nothing to check in Discord until it clears.
        self.gone_while_firing = False

    def _start(self) -> relay.Relay:
        def fire(text: str) -> str:
            if self.fire_fails:
                raise OSError("routine refused")
            self.fired.append(re.findall(r"guild/([\w-]+)", text)[0])
            return f"https://claude.ai/code/session_{len(self.fired)}"

        state = Path(self.dir.name) / "state.json"
        return relay.Relay(state, "forum", "guild", self.forum, fire, lambda: None, lambda q: "")

    def teardown(self) -> None:
        self.patch.undo()
        self.dir.cleanup()

    def post(self) -> dict[str, Any] | None:
        return self.relay.state["posts"].get("disk-filling")

    @rule(
        firing=st.lists(st.sampled_from(SERIES), unique=True),
        gap=GAPS,
        drops=st.integers(0, 3),
        fire_fails=st.booleans(),
    )
    def notify(self, firing: list[str], gap: timedelta, drops: int, fire_fails: bool) -> None:
        """Alertmanager sends the rule's series, all of them each time, again until a 200."""
        self.now += gap
        resolved = [s for s in SERIES if s not in firing][: 1 if firing else 2]
        alerts = [_alert(s, "firing") for s in firing] + [_alert(s, "resolved") for s in resolved]
        before = self.post()
        was_cleared = before is None or before["cleared"] is not None
        self.forum.fails, self.fire_fails = drops, fire_fails
        for _ in range(5):
            if self.relay.handle({"alerts": alerts}) == 200:
                break
        else:
            pytest.fail("a notification never got its 200")
        self.fire_fails = False
        if not firing and not was_cleared:
            self.gone_while_firing = False
        self.firing = firing
        self._check(cleared_now=not firing and not was_cleared, blips=drops > 0)

    @precondition(lambda self: self.post() is not None)
    @rule()
    def delete(self) -> None:
        """Colin deletes the post, a false alarm, by hand."""
        post = self.post()
        assert post
        if post["thread"] not in self.forum.deleted:
            self.deletes += 1
            self.forum.deleted.add(post["thread"])
            self.gone_while_firing = post["cleared"] is None

    @precondition(lambda self: self.post() is not None)
    @rule(text=st.sampled_from(REPORTS))
    def agent_reports(self, text: str) -> None:
        post = self.post()
        assert post
        if post["thread"] not in self.forum.deleted:
            said = self.forum.posts[post["thread"]]["said"]
            said.append({"id": f"m{len(said)}", "content": f"Fixed: a blip.\n{text}"})

    @rule()
    def restart(self) -> None:
        self.relay = self._start()

    def _check(self, cleared_now: bool, blips: bool) -> None:
        post = self.post()
        if self.firing:
            # The alert fires: its one post is open, and in Discord unless Colin deleted it since.
            assert post and post["cleared"] is None
            if self.gone_while_firing:
                return
            assert post["thread"] not in self.forum.deleted, "a return was lost to a deleted post"
            shown = self.forum.posts[post["thread"]]
            assert shown["tags"] == ["firing"] and not shown["archived"]
        elif cleared_now:
            assert post and post["cleared"] is not None
            if post["thread"] in self.forum.deleted:
                return
            shown = self.forum.posts[post["thread"]]
            assert shown["tags"] == ["cleared"]
            if blips:
                return  # an unreadable post stays in the list, erring toward Colin seeing it
            step = self.forum.report(post["thread"])
            # A cleared post leaves the list unless the agent left Colin a step, or its agent is
            # still at work and archives the post itself once it reports.
            stays = bool(step) or (step is None and post["thread"] in self.fired)
            assert shown["archived"] is not stays

    @invariant()
    def one_post_and_one_run_per_incident(self) -> None:
        # Within the reopen window, only a deleted post makes a new one; each post starts at most
        # one run, however many series fire and however often Alertmanager sends them again.
        assert len(self.forum.posts) <= 1 + self.deletes
        assert len(self.fired) == len(set(self.fired)) <= len(self.forum.posts)


def _alert(fingerprint: str, status: str) -> dict[str, Any]:
    return {
        "status": status,
        "fingerprint": fingerprint,
        "labels": {"alertname": "disk-filling", "level": "now", "service": fingerprint},
        "annotations": {"title": "Disk filling", "value": "5"},
        "startsAt": "2026-11-17T00:00:00Z",
    }


Relay.TestCase.settings = settings(
    # The same runs each time, so CI never fails on a run a pull request did not cause, and fast
    # enough for conftest.py's TEST_LIMIT.
    derandomize=True,
    database=None,
    max_examples=100,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
TestRelay = Relay.TestCase
