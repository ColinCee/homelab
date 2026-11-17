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
# The agent's report lines (homelab-oncall, "Report"), by the step each leaves Colin.
REPORTS = {
    "Needs you: nothing": "",
    "Needs you: restart it": "restart it",
    "**Needs you:** None.": "",
}
# Gaps between notifications, mostly inside the reopen window, where a return is the same incident,
# and some past it, where a return is a new one.
GAPS = st.one_of(
    st.timedeltas(min_value=timedelta(0), max_value=relay.REOPEN_WITHIN - timedelta(minutes=1)),
    st.timedeltas(min_value=relay.REOPEN_WITHIN, max_value=relay.FORGET_AFTER * 2),
)

# Discord's calls that drop in a row, mostly none.
DROPS = st.sampled_from([0, 0, 1, 2, 3])


class Forum:
    """Discord's #incidents as the relay sees it: each post's tag, whether it is archived, and its
    messages; a deleted post answers 404, and FAILS of the next calls about a post drop."""

    def __init__(self) -> None:
        self.posts: dict[str, dict[str, Any]] = {}
        self.deleted: set[str] = set()
        self.fails = 0
        self.dropped: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(self, method: str, path: str, body: dict[str, Any] | None) -> Any:  # noqa: ANN401
        if path == "/channels/forum":
            return TAGS
        if self.fails:
            self.fails -= 1
            self.dropped.append((method, path, body))
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
        # The model: the posts the relay should have opened, the one in use, when it cleared, and
        # each post's agent reports by the step they leave Colin.
        self.opened = 0
        self.thread: str | None = None
        self.cleared_at: datetime | None = None
        self.reports: dict[str, list[str]] = {}
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
        firing=st.lists(st.sampled_from(SERIES), min_size=1, unique=True),
        gap=GAPS,
        drops=DROPS,
        fire_fails=st.booleans(),
    )
    def fires(self, firing: list[str], gap: timedelta, drops: int, fire_fails: bool) -> None:
        self.notify(firing, gap, drops, fire_fails)

    @rule(gap=GAPS, drops=DROPS)
    def clears(self, gap: timedelta, drops: int) -> None:
        self.notify([], gap, drops, False)

    def notify(self, firing: list[str], gap: timedelta, drops: int, fire_fails: bool) -> None:
        """Alertmanager sends the rule's series, all of them each time, again until a 200."""
        self.now += gap
        resolved = [s for s in SERIES if s not in firing][: 1 if firing else 2]
        alerts = [_alert(s, "firing") for s in firing] + [_alert(s, "resolved") for s in resolved]
        before = self.post()
        was_cleared = before is None or before["cleared"] is not None
        self.forum.fails, self.fire_fails, self.forum.dropped = drops, fire_fails, []
        if firing and self._new_incident():
            self.opened += 1
            self.thread = f"thread-{self.opened}"
        for _ in range(5):
            if self.relay.handle({"alerts": alerts}) == 200:
                break
        else:
            pytest.fail("a notification never got its 200")
        self.fire_fails = False
        if not firing and not was_cleared:
            self.gone_while_firing = False
            self.cleared_at = self.now
        if firing:
            self.cleared_at = None
        self.firing = firing
        self._check(cleared_now=not firing and not was_cleared)

    def _new_incident(self) -> bool:
        """A firing rule's own post holds every return until a day after it clears, unless Colin
        deleted it."""
        if self.thread is None or self.thread in self.forum.deleted:
            return self.cleared_at is not None or self.thread is None
        return self.cleared_at is not None and self.now - self.cleared_at >= relay.REOPEN_WITHIN

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
    @rule(text=st.sampled_from(sorted(REPORTS)))
    def agent_reports(self, text: str) -> None:
        post = self.post()
        assert post
        if post["thread"] not in self.forum.deleted:
            said = self.forum.posts[post["thread"]]["said"]
            said.append({"id": f"m{len(said)}", "content": f"Fixed: a blip.\n{text}"})
            self.reports.setdefault(post["thread"], []).append(REPORTS[text])

    @rule()
    def restart(self) -> None:
        self.relay = self._start()

    def _check(self, cleared_now: bool) -> None:
        post = self.post()
        if self.firing:
            # The alert fires: its one post is open, and in Discord unless Colin deleted it since.
            assert post and post["cleared"] is None
            assert post["thread"] == self.thread, "a return took the wrong post"
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
            thread = post["thread"]
            steps = self.reports.get(thread)
            # A cleared post leaves the list unless the agent's newest report left Colin a step, or
            # its agent is still at work and archives the post itself once it reports.
            stays = bool(steps[-1]) if steps else thread in self.fired
            # A post Discord would not read or archive just now stays, erring toward Colin.
            unread = any(
                p.startswith(f"/channels/{thread}/messages?") for _, p, _ in self.forum.dropped
            )
            dropped = ("PATCH", f"/channels/{thread}", {"archived": True}) in self.forum.dropped
            assert shown["archived"] is not (stays or unread or dropped)

    @invariant()
    def one_post_and_one_run_per_incident(self) -> None:
        # A new post only for a return past the reopen window or after Colin deleted the post;
        # each starts at most one run, however many series fire and however often Alertmanager
        # sends them again.
        assert len(self.forum.posts) == self.opened
        assert all(len(p["said"]) <= relay.REPORT_WITHIN for p in self.forum.posts.values())
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
    # Under REPORT_WITHIN messages a post, so the agent's newest report is always in sight.
    stateful_step_count=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
TestRelay = Relay.TestCase
