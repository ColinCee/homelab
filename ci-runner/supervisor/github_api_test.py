import email.message
import http.client
from pathlib import Path
from typing import Any, ClassVar

import ci_supervisor as sup
import github_api
import pytest

from conftest import NoGitHub


class Repos(NoGitHub):
    def __init__(self, page: list[dict[str, Any]]) -> None:
        self.page = page

    def request(self, method: str, path: str, body: object = None) -> list[dict[str, Any]]:
        assert path.startswith("/user/repos?")
        return self.page


def test_serves_only_colins_private_repos_the_token_can_see() -> None:
    page = [
        {"full_name": "ColinCee/servo", "private": True, "owner": {"login": "ColinCee"}},
        {"full_name": "ColinCee/cortex", "private": True, "owner": {"login": "ColinCee"}},
        {"full_name": "ColinCee/blog", "private": False, "owner": {"login": "ColinCee"}},
        {"full_name": "someone/theirs", "private": True, "owner": {"login": "someone"}},
        {
            "full_name": "ColinCee/old-project",
            "private": True,
            "archived": True,
            "owner": {"login": "ColinCee"},
        },
    ]
    assert list(github_api.private_repos(Repos(page))) == ["ColinCee/cortex", "ColinCee/servo"]


class Pages(NoGitHub):
    def __init__(self, total: int) -> None:
        self.total, self.asked = total, 0

    def request(self, method: str, path: str, body: object = None) -> list[dict[str, Any]]:
        self.asked += 1
        start = (self.asked - 1) * 100
        return [
            {"full_name": f"ColinCee/r{i:03}", "private": True, "owner": {"login": "ColinCee"}}
            for i in range(start, min(start + 100, self.total))
        ]


def test_reads_every_page_of_repos() -> None:
    gh = Pages(150)
    assert len(github_api.private_repos(gh)) == 150 and gh.asked == 2


@pytest.mark.parametrize(
    ("labels", "ours"),
    [
        (["homelab-fc"], True),
        (["self-hosted", "linux", "x64", "homelab-fc"], True),
        (["Self-Hosted", "HOMELAB-FC"], True),
        (["self-hosted"], False),  # the VMs carry only homelab-fc
        (["homelab-fc", "beelink"], False),  # beelink is not a VM's label
    ],
)
def test_takes_only_jobs_asking_for_our_label(labels: list[str], ours: bool) -> None:
    assert github_api.wants_us(labels) is ours


def test_finds_queued_jobs_for_us_only() -> None:
    runs = [{"id": 1}, {"id": 2}]
    jobs = {
        1: [
            {
                "id": 11,
                "name": "check",
                "status": "queued",
                "labels": ["homelab-fc"],
                "created_at": "2026-11-07T10:00:00Z",
            },
            {"id": 12, "name": "windows", "status": "queued", "labels": ["windows-latest"]},
        ],
        2: [{"id": 21, "name": "check", "status": "in_progress", "labels": ["homelab-fc"]}],
    }

    found = github_api.queued_jobs("ColinCee/cortex", runs, jobs.__getitem__)

    assert [job.id for job in found] == [11]
    assert found[0].queued_at == 1794045600.0  # GitHub's created_at


@pytest.mark.parametrize(
    ("repo", "private", "approval", "served"),
    [
        ("ColinCee/cortex", True, "", True),
        ("ColinCee/cortex", False, "all_external_contributors", False),  # public, not listed
        ("ColinCee/cortex", "true", "", False),
        ("ColinCee/homelab", False, "all_external_contributors", True),
        ("ColinCee/homelab", False, "first_time_contributors", False),
        ("ColinCee/homelab", None, "all_external_contributors", False),
    ],
)
def test_serves_a_private_repo_or_a_listed_public_one_holding_runs_for_approval(
    repo: str,
    private: object,
    approval: str,
    served: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_api, "GUARD_MARKER", tmp_path / "job-guard")
    data = {"private": private, "full_name": repo}
    if private is False:
        # An image without the job guard serves no public repo.
        assert github_api.is_served(data, repo, lambda: approval) is False
    (tmp_path / "job-guard").touch()
    assert github_api.is_served(data, repo, lambda: approval) is served
    # Renamed or transferred.
    assert github_api.is_served(data | {"full_name": "someone/x"}, repo, lambda: approval) is False


def test_a_public_repo_gets_vms_only_for_colins_runs() -> None:
    colin = {"id": github_api.OWNER_ID, "login": github_api.OWNER}
    other = {"id": 1, "login": "someone"}
    assert github_api.colins({"actor": colin, "triggering_actor": colin})
    # Colin re-running someone else's run still runs their code.
    assert not github_api.colins({"actor": other, "triggering_actor": colin})
    assert not github_api.colins({"actor": colin, "triggering_actor": other})
    assert not github_api.colins({})


@pytest.mark.parametrize(
    ("retry_after", "reset", "until"),
    [("30", "5000", 1030.0), (None, "5000", 5000.0), (None, None, 1060.0)],
)
def test_backs_off_until_github_says(
    retry_after: str | None, reset: str | None, until: float
) -> None:
    assert github_api.rate_limit_until(retry_after, reset, 1000.0) == until


@pytest.mark.parametrize(
    ("label", "repo"),
    [
        ("fc-cache-octo--widgets", "octo/widgets"),
        ("FC-Cache-Octo--Widgets", "octo/widgets"),  # GitHub matches labels in any case
        ("fc-cache-octo--my--app", "octo/my--app"),  # an owner name cannot hold "--"
        ("fc-cache-octo-widgets", None),
        ("fc-cache---widgets", None),
        ("fc-cache-octo--", None),
    ],
)
def test_reads_the_repo_a_cache_label_names(label: str, repo: str | None) -> None:
    assert github_api.cache_label_repo(label) == repo


@pytest.mark.parametrize(
    ("labels", "ours"),
    [
        (["homelab-fc", "fc-cache-octo--widgets"], True),
        (["self-hosted", "homelab-fc", "fc-cache-octo--unserved"], True),  # served, no disk
        (["homelab-fc", "fc-cache-octo--a", "fc-cache-octo--b"], False),  # one VM, one disk
        (["fc-cache-octo--widgets"], False),
    ],
)
def test_takes_jobs_with_one_cache_label(labels: list[str], ours: bool) -> None:
    assert github_api.wants_us(labels) is ours


class OneRepoBroken(NoGitHub):
    """GitHub as seen by a token that cannot see `broken`."""

    def __init__(self, broken: str) -> None:
        self.broken = broken

    def request(self, method: str, path: str, body: object = None) -> dict[str, Any]:
        if path.startswith(f"/repos/{self.broken}"):
            raise OSError("HTTP Error 404: Not Found")
        if path.endswith("/runners?per_page=100"):
            return {"runners": [{"id": 7, "name": f"{github_api.RUNNER_PREFIX}0-1"}]}
        return {}


def test_stale_runners_are_cleared_where_github_answers_and_retried_where_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(
        github_api, "delete_runner", lambda gh, repo, rid, name: deleted.append(repo) or True
    )
    assert github_api.remove_stale_runners(OneRepoBroken("o/gone"), ["o/gone", "o/ok"]) == [
        "o/gone"
    ]
    assert deleted == ["o/ok"]


def test_a_lasting_failure_logs_once(
    supervisor_log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_api, "SKIPPED", {})
    for _ in range(3):
        github_api.skipped("o/gone", OSError("404"))
    github_api.skipped("o/gone", None)
    assert len(supervisor_log) == 2 and supervisor_log[1] == "o/gone is back"


def test_a_quiet_repo_listed_private_needs_no_privacy_request() -> None:
    state = sup.State(serving={"a/quiet": github_api.Serving(True, 100.0, "main")})
    assert github_api.check_served(Repos([]), state, "a/quiet", 130.0) is True


class People(NoGitHub):
    def __init__(self, logins: list[str], keys: list[dict[str, Any]] | None = None) -> None:
        self.logins, self.keys = logins, keys or []

    def request(self, method: str, path: str, body: object = None) -> list[dict[str, Any]]:
        if "/keys" in path:
            return self.keys
        return [{"login": login} for login in self.logins]


def test_a_repo_anyone_else_can_push_to_is_never_served() -> None:
    assert github_api.only_colin(People(["ColinCee"]), "a/mine") is True
    assert github_api.only_colin(People(["ColinCee", "friend"]), "a/shared") is False
    assert github_api.only_colin(People(["ColinCee"], [{"read_only": True}]), "a/key") is True
    assert github_api.only_colin(People(["ColinCee"], [{"read_only": False}]), "a/key") is False


def test_doubt_about_collaborators_is_a_no() -> None:
    assert github_api.only_colin(OneRepoBroken("a/x"), "a/x") is False


def test_a_listing_too_old_to_trust_means_asking_github() -> None:
    state = sup.State(serving={"a/quiet": github_api.Serving(True, 0.0, "main")})
    asked = []

    class Public(NoGitHub):
        def request(self, method: str, path: str, body: object = None) -> dict[str, Any]:
            asked.append(path)
            return {"private": False, "visibility": "public", "default_branch": "main"}

    assert github_api.check_served(Public(), state, "a/quiet", 3 * github_api.REPOS_EVERY) is False
    assert asked == ["/repos/a/quiet"]


def test_a_queued_jobs_workflow_comes_from_github() -> None:
    labels = list(github_api.RUNNER_LABELS)
    jobs = {
        1: [{"id": 7, "status": "queued", "labels": labels, "workflow_name": "watch"}],
        2: [{"id": 8, "status": "queued", "labels": labels}],
    }
    runs = [{"id": 1, "name": "ignored"}, {"id": 2, "name": "build"}]
    found = github_api.queued_jobs("o/ok", runs, jobs.__getitem__)
    assert [job.workflow for job in found] == ["watch", "build"]


class Connection:
    """api.github.com as seen down one connection, recording what is sent; `drop` makes
    its next request fail as if GitHub had closed it while it sat idle."""

    made: ClassVar[list[Connection]] = []

    def __init__(self, host: str, timeout: float) -> None:
        self.sent: list[str] = []
        self.drop = False
        Connection.made.append(self)

    def request(self, method: str, path: str, body: bytes | None, headers: dict[str, Any]) -> None:
        if self.drop:
            raise http.client.RemoteDisconnected("closed")
        self.sent.append(f"{method} {path}")

    def getresponse(self) -> object:
        class Reply:
            status, reason = 200, "OK"
            headers = email.message.Message()

            def read(self) -> bytes:
                return b"{}"

        return Reply()

    def close(self) -> None:
        pass


def test_requests_share_one_open_connection_and_a_closed_one_is_reopened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Connection, "made", [])
    monkeypatch.setattr(github_api.http.client, "HTTPSConnection", Connection)
    gh = github_api.GitHub("t")
    gh.request("GET", "/a")
    gh.request("GET", "/b")
    assert len(Connection.made) == 1 and Connection.made[0].sent == ["GET /a", "GET /b"]
    Connection.made[0].drop = True
    gh.request("GET", "/c")
    assert len(Connection.made) == 2 and Connection.made[1].sent == ["GET /c"]
    # A registration is never sent twice, so it never goes down a connection that may be closed.
    gh.request("POST", "/r", {})
    assert len(Connection.made) == 3 and Connection.made[2].sent == ["POST /r"]


def test_stale_runners_skip_the_vms_still_running() -> None:
    class Runners(NoGitHub):
        deleted: ClassVar[list[str]] = []

        def request(self, method: str, path: str, body: object = None) -> dict[str, Any]:
            if method == "DELETE":
                Runners.deleted.append(path.rsplit("/", 1)[1])
                return {}
            return {"runners": [{"id": 1, "name": "fc-1-5"}, {"id": 2, "name": "fc-2-5"}]}

    github_api.remove_stale_runners(Runners(), ["o/ok"], keep=["fc-2-5"])
    assert Runners.deleted == ["1"]


def test_a_queued_job_knows_its_run_already_finished_a_build() -> None:
    hosted = ["ubuntu-latest"]
    build = ["self-hosted", "linux", "x64", "homelab-fc"]
    jobs = {
        # A task's run: its build is done, its review waits.
        1: [
            {"id": 11, "status": "completed", "labels": hosted},
            {"id": 12, "status": "completed", "labels": build},
            {"id": 13, "name": "review", "status": "queued", "labels": build},
        ],
        # A new task's run: only its first step, on GitHub's runner, is done.
        2: [
            {"id": 21, "status": "completed", "labels": hosted},
            {"id": 22, "name": "build", "status": "queued", "labels": build},
        ],
    }

    found = github_api.queued_jobs("o/ok", [{"id": 1}, {"id": 2}], jobs.__getitem__)

    assert [(job.id, job.run_started) for job in found] == [(13, True), (22, False)]
