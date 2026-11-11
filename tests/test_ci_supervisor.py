import email.message
import http.client
import io
import json
import tarfile
import threading
import urllib.error
from pathlib import Path
from typing import ClassVar

import pytest

from scripts import ci_supervisor as sup

# The real readers, kept before plenty_of_memory replaces them.
READ_MEMORY, READ_RESERVE = sup.mem_available_mib, sup.memory_reserve_mib
READ_DISK_WAIT = sup.disk_wait_percent


@pytest.fixture(autouse=True)
def plenty_of_memory(monkeypatch):
    """Tests do not depend on the machine running them: memory is never short unless a
    test says so."""
    monkeypatch.setattr(sup, "mem_available_mib", lambda: 1 << 20)
    monkeypatch.setattr(sup, "memory_reserve_mib", lambda: sup.DEFAULT_MEMORY_RESERVE_MIB)
    monkeypatch.setattr(sup, "disk_wait_percent", lambda: 0.0)


@pytest.fixture(autouse=True)
def waits_file(tmp_path_factory, monkeypatch) -> Path:
    path = tmp_path_factory.mktemp("status") / "waits.jsonl"
    monkeypatch.setattr(sup, "WAITS_FILE", path)
    return path


@pytest.fixture(autouse=True)
def wait_metrics(tmp_path_factory, monkeypatch) -> Path:
    path = tmp_path_factory.mktemp("metrics") / "ci-waits.prom"
    monkeypatch.setattr(sup, "WAIT_METRICS", path)
    return path


@pytest.fixture(autouse=True)
def no_load_test(tmp_path_factory, monkeypatch) -> None:
    """The machine running the tests has no drain on and no load test record."""
    folder = tmp_path_factory.mktemp("load-test")
    monkeypatch.setattr(sup, "DRAIN_FILE", folder / "drain")
    monkeypatch.setattr(sup, "LOAD_TEST_FILE", folder / "load-test.json")


@pytest.fixture(autouse=True)
def outbox_dir(tmp_path_factory, monkeypatch) -> Path:
    path = tmp_path_factory.mktemp("outbox")
    monkeypatch.setattr(sup, "OUTBOX_DIR", path)
    return path


class Repos:
    def __init__(self, page: list[dict]) -> None:
        self.page = page

    def request(self, method: str, path: str, body: object = None) -> list[dict]:
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
    assert list(sup.served_repos(Repos(page))) == ["ColinCee/cortex", "ColinCee/servo"]


class Pages:
    def __init__(self, total: int) -> None:
        self.total, self.asked = total, 0

    def request(self, method: str, path: str, body: object = None) -> list[dict]:
        self.asked += 1
        start = (self.asked - 1) * 100
        return [
            {"full_name": f"ColinCee/r{i:03}", "private": True, "owner": {"login": "ColinCee"}}
            for i in range(start, min(start + 100, self.total))
        ]


def test_reads_every_page_of_repos() -> None:
    gh = Pages(150)
    assert len(sup.served_repos(gh)) == 150 and gh.asked == 2


def test_the_standby_goes_to_the_repo_of_the_last_job() -> None:
    repos = ["a/x", "a/y", "a/z"]
    assert sup.standby_first(repos, "a/y") == ["a/y", "a/x", "a/z"]
    assert sup.standby_first(repos, "") == repos
    assert sup.standby_first(repos, "a/gone") == repos


@pytest.mark.parametrize(
    ("labels", "ours"),
    [
        (["homelab-fc"], True),
        (["self-hosted", "linux", "x64", "homelab-fc"], True),
        (["Self-Hosted", "HOMELAB-FC"], True),
        (["self-hosted"], False),  # could be the deploy runner's job
        (["homelab-fc", "beelink"], False),  # the deploy runner's label
    ],
)
def test_takes_only_jobs_asking_for_our_label(labels: list[str], ours: bool) -> None:
    assert sup.wants_us(labels) is ours


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

    found = sup.queued_jobs("ColinCee/cortex", runs, jobs.__getitem__)

    assert [job.id for job in found] == [11]
    assert found[0].queued_at == 1794045600.0  # GitHub's created_at


@pytest.mark.parametrize(
    ("queued", "booting", "free", "starts"),
    [
        (["a/x"] * 3, {}, 2, ["a/x", "a/x"]),
        (["a/x"] * 2, {"a/x": 1}, 2, ["a/x"]),  # one VM is already booting for it
        (["b/y", "a/x"], {}, 1, ["b/y"]),  # the oldest job first, whatever its name
        (["a/x", "b/y", "a/x"], {"a/x": 1}, 1, ["b/y"]),  # a booting VM covers the oldest
        (["a/x"], {}, 0, []),
    ],
)
def test_plans_one_vm_per_uncovered_job_oldest_first(
    queued: list[str], booting: dict[str, int], free: int, starts: list[str]
) -> None:
    assert sup.plan(queued, booting, free) == starts


@pytest.mark.parametrize(
    ("data", "private"),
    [
        ({"private": True, "full_name": "ColinCee/cortex"}, True),
        ({"private": False, "full_name": "ColinCee/cortex"}, False),
        ({"private": "true", "full_name": "ColinCee/cortex"}, False),
        ({"private": True, "full_name": "someone/cortex"}, False),  # renamed or transferred
    ],
)
def test_serves_only_a_repo_that_is_still_private(data: dict[str, object], private: bool) -> None:
    assert sup.is_private(data, "ColinCee/cortex") is private


@pytest.mark.parametrize(
    ("retry_after", "reset", "until"),
    [("30", "5000", 1030.0), (None, "5000", 5000.0), (None, None, 1060.0)],
)
def test_backs_off_until_github_says(
    retry_after: str | None, reset: str | None, until: float
) -> None:
    assert sup.rate_limit_until(retry_after, reset, 1000.0) == until


@pytest.mark.parametrize(
    ("age", "queued", "check"),
    [
        (5, 0, False),  # still booting
        (30, 0, True),  # booted, and no job left for it: ask GitHub
        (300, 1, False),  # a job is queued and may still be assigned to it
    ],
)
def test_asks_about_an_idle_vm_only_once_booted_with_nothing_queued(
    age: float, queued: int, check: bool
) -> None:
    assert sup.may_be_idle(age, queued) is check


@pytest.mark.parametrize(("running", "size"), [(0, (4, 4096)), (1, (2, 2048))])
def test_a_job_alone_gets_all_four_cores(running: int, size: tuple[int, int]) -> None:
    assert sup.vm_size(running) == size


@pytest.mark.parametrize("kind", [tarfile.CHRTYPE, tarfile.FIFOTYPE])
def test_the_cache_unpacker_refuses_device_files(kind: bytes, tmp_path) -> None:
    member = tarfile.TarInfo("cache/dev")
    member.type = kind

    with pytest.raises(tarfile.FilterError):
        sup.safe_member(member, str(tmp_path))


def test_the_cache_unpacker_refuses_paths_outside_its_folder(tmp_path) -> None:
    member = tarfile.TarInfo("../../etc/cron.d/x")

    with pytest.raises(tarfile.FilterError):
        sup.safe_member(member, str(tmp_path))


def test_the_cache_unpacker_refuses_a_hard_link_out_through_a_symlink(tmp_path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "victim").write_text("host file")
    (root / "a").symlink_to("../outside")  # as if unpacked from the same archive
    link = tarfile.TarInfo("h")
    link.type, link.linkname = tarfile.LNKTYPE, "a/victim"

    with pytest.raises(tarfile.FilterError):
        sup.safe_member(link, str(root))


def test_the_cache_unpacker_refuses_an_archive_that_unpacks_too_big(tmp_path) -> None:
    check = sup.size_capped(100)
    small, big = tarfile.TarInfo("cache/a"), tarfile.TarInfo("cache/b")
    small.size, big.size = 60, 60

    check(small, str(tmp_path))
    with pytest.raises(tarfile.FilterError):
        check(big, str(tmp_path))


def _tar(*members: tuple[str, bytes | str]) -> bytes:
    """A tar of files (bytes content) and hard links (str target), in order."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for name, body in members:
            info = tarfile.TarInfo(name)
            if isinstance(body, str):
                info.type, info.linkname = tarfile.LNKTYPE, body
                tar.addfile(info)
            else:
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
    return out.getvalue()


def test_the_cache_unpacker_takes_the_later_entry_of_a_path_held_twice(tmp_path) -> None:
    # servo's cache, 5 Nov: web/node_modules twice, the second time with a hard link onto
    # a file the first had unpacked, and tarfile tried to seek back in the stream.
    archive = _tar(
        ("web/esbuild/bin/esbuild", b"js shim"),
        ("web/linux-x64/esbuild", b"binary"),
        ("web/esbuild/bin/esbuild", "web/linux-x64/esbuild"),
    )

    sup.unpack_cache(io.BytesIO(archive), tmp_path)

    assert (tmp_path / "web/esbuild/bin/esbuild").read_bytes() == b"binary"


RUNNER = "fc-1-1762400000"


def _outbox(directory: Path, header: bytes, repo: str = "octo/app") -> Path:
    """An ended VM's outbox as the fc-cache action leaves it: a header, then a tar."""
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        member = tarfile.TarInfo("uv/wheel")
        member.size = 1
        tar.addfile(member, io.BytesIO(b"w"))
    tar_path = directory / f"{RUNNER}.tar"
    tar_path.write_bytes(header.ljust(512, b"\0") + archive.getvalue())
    (directory / f"{RUNNER}.json").write_text(json.dumps({"repo": repo}))
    return tar_path


class Runs:
    """GitHub's answers about run 70, attempt 1."""

    def __init__(self, event: str = "push", branch: str = "main", job: dict | None = None):
        self.run = {"id": 70, "event": event, "head_branch": branch, "head_sha": "abc"}
        default = {"runner_name": RUNNER, "status": "completed", "conclusion": "success"}
        self.jobs = [{"runner_name": "other", "status": "completed", "conclusion": "success"}]
        self.jobs.append(default if job is None else job)
        self.asked: list[str] = []

    def request(self, method: str, path: str, body: object = None) -> dict:
        self.asked.append(path)
        if path.endswith("/jobs?per_page=100"):
            assert path.startswith("/repos/octo/app/actions/runs/70/attempts/1/")
            return {"jobs": self.jobs}
        assert path == "/repos/octo/app/actions/runs/70/attempts/1"
        return self.run


GREEN = b"homelab-ci-cache v1 run=70 attempt=1\n"


@pytest.fixture
def built(caches, monkeypatch) -> list[list[str]]:
    """What each cache disk build would have held."""
    names: list[list[str]] = []

    def build(stream, image) -> None:
        with tarfile.open(fileobj=stream, mode="r|") as tar:
            names.append([m.name for m in tar])
        image.touch()

    monkeypatch.setattr(sup, "build_cache_image", build)
    return names


def test_builds_the_cache_disk_from_a_green_default_branch_jobs_outbox(
    outbox_dir, built, caches
) -> None:
    tar = _outbox(outbox_dir, GREEN)

    assert sup.take_outbox(Runs(), tar, {"octo/app": "main"}, 0.0) is False  # done with it

    assert built == [["uv/wheel"]]
    assert json.loads((caches / "octo__app.json").read_text()) == {
        "run": 70,
        "attempt": 1,
        "sha": "abc",
    }


@pytest.mark.parametrize(
    "github",
    [
        Runs(event="pull_request"),
        Runs(branch="evil-branch"),  # a branch cannot replace the cache
        # A pull request's job naming a green main run: that run's jobs ran elsewhere.
        Runs(job={"runner_name": "fc-2-1", "status": "completed", "conclusion": "success"}),
        Runs(job={"runner_name": RUNNER, "status": "completed", "conclusion": "failure"}),
    ],
)
def test_takes_only_the_outbox_of_a_passed_job_on_a_push_to_the_default_branch(
    github, outbox_dir, built
) -> None:
    tar = _outbox(outbox_dir, GREEN)

    assert sup.take_outbox(github, tar, {"octo/app": "main"}, 0.0) is False
    assert built == []


def test_keeps_an_outbox_until_github_shows_its_job_finished(outbox_dir, built) -> None:
    tar = _outbox(outbox_dir, GREEN)
    running = Runs(job={"runner_name": RUNNER, "status": "in_progress", "conclusion": None})
    made = tar.stat().st_mtime

    assert sup.take_outbox(running, tar, {"octo/app": "main"}, made + 60) is True
    assert sup.take_outbox(running, tar, {"octo/app": "main"}, made + 3601) is False
    assert built == []


@pytest.mark.parametrize(
    "header",
    [
        b"homelab-ci-outbox\n",  # nothing saved: most VMs end so
        b"homelab-ci-cache v1 run=70 attempt=1 ; rm -rf /\n",
        b"homelab-ci-cache v1 run=/../70 attempt=1\n",
    ],
)
def test_an_outbox_without_the_actions_header_asks_github_nothing(
    header: bytes, outbox_dir, built
) -> None:
    github = Runs()

    assert sup.take_outbox(github, _outbox(outbox_dir, header), {"octo/app": "main"}, 0.0) is False
    assert github.asked == [] and built == []


def test_an_outbox_older_than_the_disks_run_is_not_taken(outbox_dir, built, caches) -> None:
    (caches / "octo__app.json").write_text(json.dumps({"run": 71, "sha": "def"}))

    assert sup.take_outbox(Runs(), _outbox(outbox_dir, GREEN), {"octo/app": "main"}, 0.0) is False
    assert built == []


def test_an_outbox_of_a_repo_no_longer_served_is_not_taken(outbox_dir, built) -> None:
    github = Runs()

    assert sup.take_outbox(github, _outbox(outbox_dir, GREEN), {}, 0.0) is False
    assert github.asked == [] and built == []


def test_judges_only_the_outboxes_of_vms_that_ended(outbox_dir) -> None:
    for name in ("fc-0-1", "fc-1-2", "fc-2-3"):
        (outbox_dir / f"{name}.json").write_text("{}")
    state = sup.State()
    state.launches[0] = sup.Launch(0, "o/r", 1, "fc-0-1", 0.0, Path("m"))
    state.cache_checked["fc-2-3"] = 100.0  # tried 30 s ago

    assert sup.ended_outboxes(state, 130.0) == [outbox_dir / "fc-1-2.tar"]


class Failing:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def request(self, method: str, path: str, body: object = None) -> dict:
        raise self.error


@pytest.mark.parametrize(
    ("error", "kept"),
    [
        (urllib.error.HTTPError("u", 404, "Not Found", email.message.Message(), None), False),
        (urllib.error.HTTPError("u", 502, "Bad Gateway", email.message.Message(), None), True),
        (sup.RateLimited(0.0), True),
    ],
)
def test_keeps_an_outbox_only_while_github_may_answer_later(
    error: Exception, kept: bool, outbox_dir, built
) -> None:
    tar = _outbox(outbox_dir, GREEN)

    assert sup.judge_outbox(Failing(error), tar, {"octo/app": "main"}, tar.stat().st_mtime) is kept
    assert tar.exists() is kept and tar.with_suffix(".json").exists() is kept


def test_a_cache_tar_that_fails_to_unpack_is_dropped(outbox_dir, caches) -> None:
    tar = _outbox(outbox_dir, GREEN)
    tar.write_bytes(GREEN.ljust(512, b"\0") + b"not a tar")

    assert sup.take_outbox(Runs(), tar, {"octo/app": "main"}, 0.0) is False
    assert not sup.cache_image("octo/app").exists()


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
    assert sup.cache_label_repo(label) == repo


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
    assert sup.wants_us(labels) is ours


def test_keeps_jobs_with_different_cache_labels_apart() -> None:
    plain = sup.Job("octo/agent", 1, "check", ("homelab-fc",))
    labelled = sup.Job("octo/agent", 2, "build", ("homelab-fc", "FC-CACHE-octo--widgets"))
    assert plain.want == ("octo/agent", "")
    assert labelled.want == ("octo/agent", "fc-cache-octo--widgets")

    # A standby (or booting VM) without the label does not cover the labelled job.
    starts = sup.plan([plain.want, labelled.want], {plain.want: 1}, 2)

    assert starts == [labelled.want]


@pytest.fixture
def caches(tmp_path, monkeypatch):
    monkeypatch.setattr(sup, "CACHE_DIR", tmp_path)
    (tmp_path / "octo__widgets.ext4").touch()
    return tmp_path


def test_a_job_without_a_cache_label_gets_its_own_repos_disk(caches) -> None:
    assert sup.cache_for("octo/agent", "", ["octo/agent"]) == caches / "octo__agent.ext4"


def test_a_cache_label_naming_a_served_private_repo_gets_its_disk(caches) -> None:
    disk = sup.cache_for("octo/agent", "FC-CACHE-octo--widgets", ["octo/agent", "octo/widgets"])

    assert disk == caches / "octo__widgets.ext4"


def test_a_cache_label_naming_an_unserved_or_public_repo_gets_no_disk(caches) -> None:
    # widgets has a disk but is not in this poll's private list; nodisk is, but has no disk.
    private = ["octo/agent", "octo/nodisk"]
    assert sup.cache_for("octo/agent", "fc-cache-octo--widgets", private) is None
    assert sup.cache_for("octo/agent", "fc-cache-octo--nodisk", private) is None


class OneRepoBroken:
    """GitHub as seen by a token that cannot see `broken`."""

    def __init__(self, broken: str) -> None:
        self.broken = broken

    def request(self, method: str, path: str, body: object = None) -> dict:
        if path.startswith(f"/repos/{self.broken}"):
            raise OSError("HTTP Error 404: Not Found")
        if path.endswith("/runners?per_page=100"):
            return {"runners": [{"id": 7, "name": f"{sup.RUNNER_PREFIX}0-1"}]}
        return {}


def test_stale_runners_are_cleared_where_github_answers_and_retried_where_not(monkeypatch) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(
        sup, "delete_runner", lambda gh, repo, rid, name: deleted.append(repo) or True
    )
    assert sup.remove_stale_runners(OneRepoBroken("o/gone"), ["o/gone", "o/ok"]) == ["o/gone"]
    assert deleted == ["o/ok"]


@pytest.mark.parametrize("fails", ["check_private", "find_queued"])
def test_a_repo_github_will_not_show_never_stops_the_others(monkeypatch, fails: str) -> None:
    def broken(repo: str, value):
        if repo == "o/gone":
            raise OSError("HTTP Error 404: Not Found")
        return value

    monkeypatch.setattr(
        sup,
        "check_private",
        lambda gh, state, repo, now, fresh=False: (
            broken(repo, True) if fails == "check_private" else True
        ),
    )
    job = sup.Job("o/ok", 1, "check", tuple(sup.RUNNER_LABELS))
    monkeypatch.setattr(
        sup,
        "find_queued",
        lambda gh, repo: (
            broken(repo, [job] if repo == "o/ok" else [])
            if fails == "find_queued"
            else ([job] if repo == "o/ok" else [])
        ),
    )
    monkeypatch.setattr(sup, "reap", lambda *a, **k: None)
    monkeypatch.setattr(sup, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    launched: list[str] = []
    monkeypatch.setattr(
        sup, "launch", lambda gh, state, repo, slot, now, **k: launched.append(repo)
    )
    sup.tick(OneRepoBroken("o/gone"), sup.State(), ["o/gone", "o/ok"], 0.0)
    assert launched == ["o/ok"]


def test_a_rate_limit_still_ends_the_poll(monkeypatch) -> None:
    def limited(gh, state, repo, now):
        raise sup.RateLimited(0.0)

    monkeypatch.setattr(sup, "check_private", limited)
    with pytest.raises(sup.RateLimited):
        sup.tick(OneRepoBroken("o/gone"), sup.State(), ["o/ok"], 0.0)


def test_a_lasting_failure_logs_once(monkeypatch) -> None:
    lines: list[str] = []
    monkeypatch.setattr(sup, "log", lines.append)
    monkeypatch.setattr(sup, "SKIPPED", {})
    for _ in range(3):
        sup.skipped("o/gone", OSError("404"))
    sup.skipped("o/gone", None)
    assert len(lines) == 2 and lines[1] == "o/gone is back"


def test_quiet_repos_are_polled_once_a_minute_used_ones_every_poll(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sup, "CACHE_DIR", tmp_path)
    state = sup.State(used={"a/busy"}, polled={"a/quiet": 100.0, "a/busy": 100.0})
    assert sup.due(["a/busy", "a/quiet", "a/new"], state, 130.0) == ["a/busy", "a/new"]
    assert sup.due(["a/busy", "a/quiet"], state, 161.0) == ["a/busy", "a/quiet"]


def test_a_poll_asks_only_a_few_quiet_repos_the_longest_unasked_first(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(sup, "CACHE_DIR", tmp_path)
    quiet = [f"a/q{i}" for i in range(10)]
    state = sup.State(used={"a/busy"}, polled={r: 100.0 - i for i, r in enumerate(quiet)})
    polling = sup.due(["a/busy", *quiet], state, 200.0)
    assert polling == ["a/busy", "a/q7", "a/q8", "a/q9"]
    assert len(polling) == 1 + sup.QUIET_PER_POLL


def test_after_a_restart_the_standby_goes_where_the_last_job_ran(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sup, "LAST_REPO_FILE", tmp_path / "last-repo")
    assert sup.recalled_last_job() == ("", "")
    sup.remember_last_job("a/y", "fc-cache-a--z")
    assert sup.recalled_last_job() == ("a/y", "fc-cache-a--z")
    sup.remember_last_job("a/y", "")
    last, _ = sup.recalled_last_job()
    assert last == "a/y"
    repos = ["a/x", "a/y", "a/z"]
    state = sup.State(used={last})
    # Polled every time, first, so the standby boots for it and not for the first listed.
    assert sup.due(sup.standby_first(repos, last), state, 0.0)[0] == "a/y"


def test_a_slow_poll_says_what_it_spent_its_time_on() -> None:
    line = sup.slow_poll(9.0, 40, 8.5, {"a/x": 1.0, "a/y": 6.0, "a/z": 0.5, "a/w": 0.2})
    assert line == (
        "slow poll: 9.0s; 40 GitHub requests took 8.5s in all; asking 4 repos for queued jobs "
        "at once took 6.0s (slowest a/y 6.0s, a/x 1.0s, a/z 0.5s); the rest 3.0s"
    )


def test_a_quiet_repo_listed_private_needs_no_privacy_request() -> None:
    state = sup.State(privacy={"a/quiet": sup.Privacy(True, 100.0, "main")})
    assert sup.check_private(Repos([]), state, "a/quiet", 130.0) is True


class People:
    def __init__(self, logins: list[str], keys: list[dict] | None = None) -> None:
        self.logins, self.keys = logins, keys or []

    def request(self, method: str, path: str, body: object = None) -> list[dict]:
        if "/keys" in path:
            return self.keys
        return [{"login": login} for login in self.logins]


def test_a_repo_anyone_else_can_push_to_is_never_served() -> None:
    assert sup.only_colin(People(["ColinCee"]), "a/mine") is True
    assert sup.only_colin(People(["ColinCee", "friend"]), "a/shared") is False
    assert sup.only_colin(People(["ColinCee"], [{"read_only": True}]), "a/key") is True
    assert sup.only_colin(People(["ColinCee"], [{"read_only": False}]), "a/key") is False


def test_doubt_about_collaborators_is_a_no() -> None:
    assert sup.only_colin(OneRepoBroken("a/x"), "a/x") is False


def test_a_listing_too_old_to_trust_means_asking_github() -> None:
    state = sup.State(privacy={"a/quiet": sup.Privacy(True, 0.0, "main")})
    asked = []

    class Public:
        def request(self, method: str, path: str, body: object = None) -> dict:
            asked.append(path)
            return {"private": False, "visibility": "public", "default_branch": "main"}

    assert sup.check_private(Public(), state, "a/quiet", 3 * sup.REPOS_EVERY) is False
    assert asked == ["/repos/a/quiet"]


def _launch(slot: int, standby: bool) -> sup.Launch:
    # Only the counts are read: no process or registration file is needed.
    return sup.Launch(slot, "octo/agent", slot, f"fc-{slot}", 0.0, None, standby)  # type: ignore[arg-type]


def test_the_status_file_counts_busy_and_standby_vms_and_cache_disks(caches) -> None:
    (caches / "octo__agent.ext4").touch()
    (caches / "octo__agent.json").touch()  # which artifact built it, not a disk
    state = sup.State(
        slots=5, launches={0: _launch(0, False), 1: _launch(1, False), 2: _launch(2, True)}
    )

    state.held = {7: "octo/agent"}

    assert sup.status(state, 1700000000.5, github_ok=True, mem_available=9000) == {
        "updated": 1700000000,
        "slots": 5,
        "in_use": 2,
        "standby": 1,
        "cache_disks": ["octo/agent", "octo/widgets"],
        "github_ok": True,
        "mem_available_mib": 9000,
        "held_for_memory": 1,
        "queued": 0,
        "in_use_repos": {"octo/agent": 2},
        "draining": False,
        "load_test": None,
    }


@pytest.mark.parametrize(
    ("text", "slots"),
    [(None, 5), ("four\n", 5), ("-2\n", 5), ("0\n", 1), (" 2 ", 2)],
)
def test_the_slot_count_comes_from_the_host_file(tmp_path, monkeypatch, text, slots) -> None:
    path = tmp_path / "slots"
    if text is not None:
        path.write_text(text)
    monkeypatch.setattr(sup, "SLOTS_FILE", path)
    assert sup.slot_count() == slots


def test_an_unreadable_slot_file_means_the_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sup, "SLOTS_FILE", tmp_path)  # a folder: reading it fails
    assert sup.slot_count() == 5


def test_vms_start_only_in_the_hosts_slots(caches, monkeypatch) -> None:
    monkeypatch.setattr(sup, "check_private", lambda *a, **k: True)
    monkeypatch.setattr(sup, "only_colin", lambda *a, **k: True)
    monkeypatch.setattr(
        sup,
        "find_queued",
        lambda gh, repo: [sup.Job(repo, n, "j", tuple(sup.RUNNER_LABELS)) for n in range(9)],
    )
    monkeypatch.setattr(sup, "reap", lambda *a, **k: None)
    monkeypatch.setattr(sup, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    slots: list[int] = []
    monkeypatch.setattr(sup, "launch", lambda gh, state, repo, slot, now, **k: slots.append(slot))
    sup.tick(None, sup.State(slots=5), ["o/ok"], 0.0)  # type: ignore[arg-type]
    assert slots == [0, 1, 2, 3, 4]


def test_the_status_file_is_replaced_whole_and_readable_by_all(tmp_path) -> None:
    path = tmp_path / "status" / "machines.json"
    sup.write_status(path, {"in_use": 1})
    sup.write_status(path, {"in_use": 2})

    assert json.loads(path.read_text()) == {"in_use": 2}
    assert path.stat().st_mode & 0o777 == 0o644
    assert path.parent.stat().st_mode & 0o777 == 0o755
    assert [p.name for p in path.parent.iterdir()] == ["machines.json"]  # no temp file left


def test_a_failed_status_write_leaves_the_old_file_and_no_temp_file(tmp_path) -> None:
    path = tmp_path / "machines.json"
    sup.write_status(path, {"in_use": 1})

    with pytest.raises(TypeError):
        sup.write_status(path, {"in_use": object()})

    assert json.loads(path.read_text()) == {"in_use": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["machines.json"]


def test_the_console_reader_drops_over_long_lines_and_reads_to_the_end() -> None:
    first = b"HOMELAB-MEM 1 812 1985 0\n"
    over_long = b"HOMELAB-MEM 1 990 1985 0" + b" " * 5000 + b"\n"
    console = io.BytesIO(b"Linux version 6.1\n" + first + over_long + b"login:")
    kept: list[dict] = []
    sup.read_console(console, kept.append)
    assert [line["peak"] for line in kept] == [812]
    assert console.closed


def test_every_vm_reports_its_memory(monkeypatch) -> None:
    started: list[list[str]] = []
    monkeypatch.setattr(sup.subprocess, "run", lambda cmd, **k: started.append(cmd))
    run = sup.Launch(1, "ColinCee/servo", 2, "fc-1", 0.0, Path("m1"))
    START_VM(run, 60, {})
    assert started[0][-2:] == ["--memory", str(run.memory_file)]


def test_reads_available_memory_and_the_reserve_from_the_host(tmp_path, monkeypatch) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16000000 kB\nMemAvailable:    8192000 kB\n")
    monkeypatch.setattr(sup, "MEMINFO", meminfo)
    reserve = tmp_path / "memory-reserve-mib"
    monkeypatch.setattr(sup, "MEMORY_RESERVE_FILE", reserve)
    assert READ_MEMORY() == 8000
    assert READ_RESERVE() == 3072  # no file: the default
    reserve.write_text("2048\n")
    assert READ_RESERVE() == 2048
    reserve.write_text("lots\n")
    assert READ_RESERVE() == 3072


@pytest.fixture
def host(caches, monkeypatch):
    """One served repo with jobs queued, and a launch that records the VMs it starts."""
    monkeypatch.setattr(sup, "check_private", lambda *a, **k: True)
    monkeypatch.setattr(sup, "only_colin", lambda *a, **k: True)
    monkeypatch.setattr(sup, "reap", lambda *a, **k: None)
    monkeypatch.setattr(sup, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(sup, "has_job", lambda gh, run: False)  # VMs are still starting
    lines: list[str] = []
    monkeypatch.setattr(sup, "log", lines.append)
    started: list[tuple[str, int, bool]] = []

    def launch(gh, state, repo, slot, now, *, cache, label="", standby=False, jobs=()) -> None:
        mem = sup.next_size(state, standby)[1]
        started.append((repo, mem, standby))
        run = _launch(slot, standby)
        run.repo, run.label = repo, label
        state.launches[slot] = run

    monkeypatch.setattr(sup, "launch", launch)

    def queue(jobs: int, free_mib: int, queued_at: float = 0.0, workflow: str = "") -> None:
        monkeypatch.setattr(
            sup,
            "find_queued",
            lambda gh, repo: (
                [
                    sup.Job(
                        repo,
                        n,
                        "j",
                        tuple(sup.RUNNER_LABELS),
                        queued_at=queued_at,
                        workflow=workflow,
                    )
                    for n in range(jobs)
                ]
                if repo == "o/ok"
                else []
            ),
        )
        monkeypatch.setattr(sup, "mem_available_mib", lambda: free_mib)

    return queue, started, lines


def test_vms_start_while_memory_leaves_the_reserve(host, monkeypatch) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    queue(jobs=3, free_mib=3072 + 4096 + 2048 + 2048)
    state = sup.State()
    sup.tick(None, state, ["o/ok"], 0.0)  # type: ignore[arg-type]
    assert started == [("o/ok", 4096, False), ("o/ok", 2048, False), ("o/ok", 2048, False)]
    assert state.held == {}


def test_a_job_waits_queued_when_memory_is_short_and_is_logged_once(host, monkeypatch) -> None:
    queue, started, lines = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    queue(jobs=2, free_mib=3072 + 4096 + 2047)  # room for the first VM only
    state = sup.State()
    sup.tick(None, state, ["o/ok"], 0.0)  # type: ignore[arg-type]
    assert started == [("o/ok", 4096, False)]
    assert state.held == {1: "o/ok"}

    queue(jobs=2, free_mib=3072 + 2047)  # still short, the first job's VM is still booting
    sup.tick(None, state, ["o/ok"], 2.0)  # type: ignore[arg-type]
    assert started == [("o/ok", 4096, False)]
    assert [line for line in lines if "memory" in line] == [
        "job 1 in o/ok has waited 0m00s here: memory"
        " (2047 MiB free above the 3072 MiB reserve, a VM needs 2048)"
    ]
    status = sup.status(state, 2.0, github_ok=True, mem_available=5119)
    assert status["held_for_memory"] == 1 and status["mem_available_mib"] == 5119

    queue(jobs=2, free_mib=3072 + 2048)  # memory freed: the job starts
    sup.tick(None, state, ["o/ok"], 4.0)  # type: ignore[arg-type]
    assert started[-1] == ("o/ok", 2048, False)
    assert state.held == {}


def test_no_vm_starts_while_the_disk_is_busy(host, tmp_path, monkeypatch) -> None:
    # The Beelink's /proc/pressure/io at 21:28 on 5 Nov, with 9 VMs and Grafana failing.
    pressure = tmp_path / "io"
    pressure.write_text(
        "some avg10=92.99 avg60=90.49 avg300=86.69 total=53934832649\n"
        "full avg10=27.79 avg60=26.06 avg300=27.47 total=33010242903\n"
    )
    monkeypatch.setattr(sup, "IO_PRESSURE", pressure)
    monkeypatch.setattr(sup, "disk_wait_percent", READ_DISK_WAIT)
    queue, started, lines = host
    monkeypatch.setattr(sup, "standby_count", lambda: 1)
    queue(jobs=1, free_mib=1 << 20)
    state = sup.State()
    sup.tick(None, state, ["o/ok"], 0.0)  # type: ignore[arg-type]
    assert started == [] and state.held == {0: "o/ok"}
    assert lines[-1] == (
        "job 0 in o/ok has waited 0m00s here: the disk (tasks waited on it 90% of the last minute)"
    )

    pressure.write_text("some avg10=12.00 avg60=13.63 avg300=13.00 total=1\n")
    sup.tick(None, state, ["o/ok"], 2.0)  # type: ignore[arg-type]
    assert started[0][0] == "o/ok" and state.held == {}


def test_a_half_busy_disk_starts_one_vm_a_minute(host, monkeypatch) -> None:
    queue, started, lines = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    monkeypatch.setattr(sup, "disk_wait_percent", lambda: 40.0)
    queue(jobs=3, free_mib=1 << 20)
    state = sup.State()
    sup.tick(None, state, ["o/ok"], 0.0)  # type: ignore[arg-type]
    assert len(started) == 1
    assert (
        "job 1 in o/ok has waited 0m00s here: the disk (tasks waited on it 40% of the last minute)"
    ) in lines
    sup.tick(None, state, ["o/ok"], 30.0)  # type: ignore[arg-type]
    assert len(started) == 1  # the first VM's load has not shown yet
    sup.tick(None, state, ["o/ok"], 61.0)  # type: ignore[arg-type]
    assert len(started) == 2


def test_a_job_with_no_free_slot_is_logged_with_its_wait_every_few_minutes(host, monkeypatch):
    queue, started, lines = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    monkeypatch.setattr(sup, "on_its_way", lambda *a: False)  # the first VM has its job
    queue(jobs=2, free_mib=1 << 20)
    state = sup.State(slots=1)
    sup.tick(None, state, ["o/ok"], 0.0)  # type: ignore[arg-type]
    second = [sup.Job("o/ok", 1, "j", tuple(sup.RUNNER_LABELS))]  # the first has its VM
    monkeypatch.setattr(sup, "find_queued", lambda gh, repo: second if repo == "o/ok" else [])
    for now in (2.0, 179.0, 200.0):
        sup.tick(None, state, ["o/ok"], now)  # type: ignore[arg-type]
    assert len(started) == 1
    assert lines == [
        "job 1 in o/ok has waited 0m00s here: all 1 build slots in use",
        "job 1 in o/ok has waited 3m20s here: all 1 build slots in use",
    ]
    queue(jobs=0, free_mib=1 << 20)
    sup.tick(None, state, ["o/ok"], 230.0)  # type: ignore[arg-type]
    assert lines[-1] == "job 1 in o/ok stopped waiting after 3m50s"


def test_a_job_whose_vm_is_booting_is_logged_only_after_a_long_wait(host, monkeypatch):
    queue, started, lines = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    monkeypatch.setattr(sup, "on_its_way", lambda *a: True)  # GitHub never gives it the job
    queue(jobs=1, free_mib=1 << 20)
    state = sup.State()
    for now in (0.0, 2.0, 100.0):
        sup.tick(None, state, ["o/ok"], now)  # type: ignore[arg-type]
    assert len(started) == 1 and lines == []
    sup.tick(None, state, ["o/ok"], 182.0)  # type: ignore[arg-type]
    assert lines == [f"job 0 in o/ok has waited 3m02s here: {sup.BOOTING}"]


def test_a_job_whose_vm_is_ready_is_counted_apart_from_its_boot(
    host, waits_file, monkeypatch
) -> None:
    queue, _, lines = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)

    def online(gh, run) -> bool:  # the runner is up and idle from the second poll
        run.ready = True
        return False

    monkeypatch.setattr(sup, "has_job", online)
    state = sup.State()
    queue(jobs=1, free_mib=1 << 20)
    sup.tick(None, state, ["o/ok"], 0.0)  # type: ignore[arg-type]  # its VM boots
    sup.tick(None, state, ["o/ok"], 10.0)  # type: ignore[arg-type]
    assert state.waits[0].why == sup.READY
    queue(jobs=0, free_mib=1 << 20)  # GitHub gave it the job
    sup.tick(None, state, ["o/ok"], 40.0)  # type: ignore[arg-type]
    record = json.loads(waits_file.read_text())
    assert (record["booting"], record["ready"]) == (10, 30)
    assert lines == []  # a short wait is not logged


def test_a_job_github_queued_long_before_it_was_seen_is_logged_at_once(host, monkeypatch):
    queue, _, lines = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    state = sup.State()
    queue(jobs=1, free_mib=1 << 20, queued_at=1000.0)
    sup.tick(None, state, ["o/ok"], 1600.0)  # type: ignore[arg-type]
    queue(jobs=0, free_mib=1 << 20)
    sup.tick(None, state, ["o/ok"], 1610.0)  # type: ignore[arg-type]
    assert lines == [
        f"job 0 in o/ok has waited 0m00s here (GitHub had queued it 10m00s before): {sup.BOOTING}",
        "job 0 in o/ok stopped waiting after 0m10s, 10m10s since GitHub queued it",
    ]


def test_a_held_job_is_recorded_with_its_seconds_per_kind_when_it_starts(
    host, waits_file, monkeypatch
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    monkeypatch.setattr(sup, "on_its_way", lambda *a: True)
    state = sup.State()
    queue(jobs=1, free_mib=3072 + 2047)  # held for memory
    sup.tick(None, state, ["o/ok"], 100.0)  # type: ignore[arg-type]
    sup.tick(None, state, ["o/ok"], 130.0)  # type: ignore[arg-type]
    monkeypatch.setattr(sup, "disk_wait_percent", lambda: 90.0)
    sup.tick(None, state, ["o/ok"], 160.0)  # type: ignore[arg-type]  # held for the disk
    monkeypatch.setattr(sup, "disk_wait_percent", lambda: 0.0)
    queue(jobs=1, free_mib=1 << 20)
    sup.tick(None, state, ["o/ok"], 170.0)  # type: ignore[arg-type]  # its VM boots
    assert len(started) == 1 and not waits_file.exists()
    sup.tick(None, state, ["o/ok"], 185.0)  # type: ignore[arg-type]
    queue(jobs=0, free_mib=1 << 20)  # the job started
    sup.tick(None, state, ["o/ok"], 200.0)  # type: ignore[arg-type]
    assert [json.loads(line) for line in waits_file.read_text().splitlines()] == [
        {"job": 0, "repo": "o/ok", "since": 100, "until": 200,
         "memory": 60, "disk": 10, "slots": 0, "booting": 30, "ready": 0, "unseen": 0,
         "other": 0, "why": "memory (2047 MiB free above the 3072 MiB reserve, a VM needs 4096)"}
    ]  # fmt: skip
    assert waits_file.stat().st_mode & 0o777 == 0o644


def test_a_job_a_standby_takes_is_recorded_with_its_seconds_and_reason(
    host, waits_file, monkeypatch
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    monkeypatch.setattr(sup, "on_its_way", lambda *a: False)  # the busy VM has its own job
    queue(jobs=1, free_mib=1 << 20)
    state = sup.State(slots=1, launches={0: _launch(0, False)})
    state.launches[0].repo = "o/ok"
    sup.tick(None, state, ["o/ok"], 0.0)  # type: ignore[arg-type]
    sup.tick(None, state, ["o/ok"], 100.0)  # type: ignore[arg-type]
    # That VM ends and a standby, booted before the job queued, is up in its slot.
    standby = state.launches[0] = _launch(0, True)
    standby.repo, standby.ready = "o/ok", True
    monkeypatch.setattr(sup, "on_its_way", lambda *a: True)
    sup.tick(None, state, ["o/ok"], 102.0)  # type: ignore[arg-type]
    queue(jobs=0, free_mib=1 << 20)  # GitHub gave the standby the job
    sup.tick(None, state, ["o/ok"], 104.0)  # type: ignore[arg-type]
    record = json.loads(waits_file.read_text())
    assert started == [] and (record["job"], record["slots"], record["ready"]) == (0, 102, 2)
    assert record["why"] == "all 1 build slots in use"


def test_a_job_taken_by_a_vm_booted_for_another_is_recorded_with_its_seconds_and_reason(
    host, waits_file, monkeypatch
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    monkeypatch.setattr(sup, "on_its_way", lambda *a: True)
    queue(jobs=2, free_mib=1 << 20)
    state = sup.State(slots=1)
    sup.tick(None, state, ["o/ok"], 0.0)  # type: ignore[arg-type]  # a VM boots for job 0
    sup.tick(None, state, ["o/ok"], 50.0)  # type: ignore[arg-type]
    assert len(started) == 1 and state.waits[1].why == "all 1 build slots in use"
    # GitHub gives that VM job 1, not job 0 it booted for.
    first = [sup.Job("o/ok", 0, "j", tuple(sup.RUNNER_LABELS))]
    monkeypatch.setattr(sup, "find_queued", lambda gh, repo: first if repo == "o/ok" else [])
    sup.tick(None, state, ["o/ok"], 60.0)  # type: ignore[arg-type]
    record = json.loads(waits_file.read_text())
    assert (record["job"], record["slots"], record["why"]) == (1, 60, "all 1 build slots in use")


def test_the_longest_waits_seconds_per_kind_go_to_prometheus(host, wait_metrics, monkeypatch):
    queue, _, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    monkeypatch.setattr(sup, "on_its_way", lambda *a: True)
    state = sup.State()
    # Held for memory, GitHub's for 60 s.
    queue(jobs=1, free_mib=3072 + 2047, queued_at=40.0, workflow="watch")
    sup.tick(None, state, ["o/ok"], 100.0)  # type: ignore[arg-type]
    assert 'homelab_ci_wait_seconds{kind="memory"} 0' in wait_metrics.read_text()
    sup.tick(None, state, ["o/ok"], 110.0)  # type: ignore[arg-type]  # under 15 s: not written
    queue(jobs=1, free_mib=1 << 20, queued_at=40.0)
    sup.tick(None, state, ["o/ok"], 130.0)  # type: ignore[arg-type]  # its VM boots
    sup.tick(None, state, ["o/ok"], 150.0)  # type: ignore[arg-type]
    slots = state.slots
    assert wait_metrics.read_text().splitlines() == [
        'homelab_ci_wait_seconds{kind="memory"} 30',
        'homelab_ci_wait_seconds{kind="disk"} 0',
        'homelab_ci_wait_seconds{kind="slots"} 0',
        'homelab_ci_wait_seconds{kind="booting"} 20',
        'homelab_ci_wait_seconds{kind="ready"} 0',
        'homelab_ci_wait_seconds{kind="unseen"} 60',
        'homelab_ci_wait_seconds{kind="other"} 0',
        'homelab_ci_queued_seconds{repo="o/ok",workflow="watch"} 110',
        "homelab_ci_vms_busy 1",
        f"homelab_ci_vm_slots {slots}",
        "homelab_ci_poll_failures 0",
        "homelab_ci_poll_failed_seconds 0",
    ]
    assert wait_metrics.stat().st_mode & 0o777 == 0o644
    queue(jobs=0, free_mib=1 << 20)  # the job started: nothing waits
    sup.tick(None, state, ["o/ok"], 170.0)  # type: ignore[arg-type]
    lines = wait_metrics.read_text().splitlines()
    assert {line.split()[1] for line in lines if "wait_seconds" in line} == {"0"}
    assert not [line for line in lines if "queued_seconds" in line]


def test_failed_polls_in_a_row_go_to_prometheus_and_a_good_poll_clears_them(wait_metrics) -> None:
    state = sup.State()

    def metric(name: str) -> str:
        line = next(x for x in wait_metrics.read_text().splitlines() if x.startswith(name + " "))
        return line.split()[1]

    for now in (100.0, 102.0, 104.0):
        sup.note_poll(state, False, now)
    assert metric("homelab_ci_poll_failures") == "1"  # written when polls start failing
    sup.note_poll(state, False, 120.0)  # 15 s on: written again
    assert metric("homelab_ci_poll_failures") == "4"
    assert metric("homelab_ci_poll_failed_seconds") == "20"
    sup.note_poll(state, True, 122.0)  # written at once when polls work again
    assert metric("homelab_ci_poll_failures") == "0"
    assert metric("homelab_ci_poll_failed_seconds") == "0"
    assert state.poll_failed_since is None


def test_a_queued_jobs_workflow_comes_from_github() -> None:
    labels = list(sup.RUNNER_LABELS)
    jobs = {
        1: [{"id": 7, "status": "queued", "labels": labels, "workflow_name": "watch"}],
        2: [{"id": 8, "status": "queued", "labels": labels}],
    }
    runs = [{"id": 1, "name": "ignored"}, {"id": 2, "name": "build"}]
    found = sup.queued_jobs("o/ok", runs, jobs.__getitem__)
    assert [job.workflow for job in found] == ["watch", "build"]


def test_a_label_value_is_escaped() -> None:
    assert sup.label_value('a "b" \\ c') == 'a \\"b\\" \\\\ c'


def test_an_unwritable_wait_metrics_file_is_logged_once(tmp_path, monkeypatch) -> None:
    lines: list[str] = []
    monkeypatch.setattr(sup, "log", lines.append)
    monkeypatch.setattr(sup, "WAIT_METRICS", tmp_path / "missing" / "ci-waits.prom")
    state = sup.State()
    for now in (0.0, 2.0):
        sup.note_waits(state, ["o/ok"], {}, now)
    assert len(lines) == 1 and lines[0].startswith("could not write")


def test_each_reason_has_a_kind_of_wait() -> None:
    assert sup.wait_kind("memory (0 MiB free above the 3072 MiB reserve, a VM needs 2048)") == (
        "memory"
    )
    assert sup.wait_kind("the disk (tasks waited on it 90% of the last minute)") == "disk"
    assert sup.wait_kind("all 5 build slots in use") == "slots"
    assert sup.wait_kind(sup.BOOTING) == "booting"
    assert sup.wait_kind(sup.READY) == "ready"
    assert sup.wait_kind("no VM has its labels (homelab-fc, gpu)") == "other"


def test_waits_over_30_days_old_are_pruned_once_an_hour(waits_file) -> None:
    day = 24 * 3600.0
    now = 100 * day
    old = {"job": 1, "repo": "o/ok", "since": 0, "until": int(now - 31 * day)}
    waits_file.write_text(json.dumps(old) + "\nhalf a li")
    state = sup.State()
    sup.record_wait(state, 2, sup.Wait("o/ok", now - 5, seen=now), now)
    assert [json.loads(line)["job"] for line in waits_file.read_text().splitlines()] == [2]
    with waits_file.open("a") as f:
        f.write(json.dumps(old) + "\n")
    sup.record_wait(state, 3, sup.Wait("o/ok", now, seen=now), now + 60)  # pruned an hour ago
    assert [json.loads(line)["job"] for line in waits_file.read_text().splitlines()] == [2, 1, 3]


def test_an_unwritable_waits_file_is_logged_once_and_does_not_raise(tmp_path, monkeypatch) -> None:
    lines: list[str] = []
    monkeypatch.setattr(sup, "log", lines.append)
    monkeypatch.setattr(sup, "WAITS_FILE", tmp_path / "missing" / "waits.jsonl")
    state = sup.State()
    for job in (1, 2):
        sup.record_wait(state, job, sup.Wait("o/ok", 0.0), 10.0)
    assert len(lines) == 1 and lines[0].startswith("could not write")


def test_a_job_asking_for_a_label_no_vm_has_is_logged_named_and_never_started(
    host, wait_metrics, monkeypatch
):
    _, started, lines = host
    runs = [{"id": 1}]
    jobs = {1: [{"id": 7, "name": "gpu", "status": "queued", "labels": ["homelab-fc", "gpu"]}]}
    found = sup.queued_jobs("o/ok", runs, jobs.__getitem__)
    assert [(job.id, job.ours) for job in found] == [(7, False)]
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    monkeypatch.setattr(sup, "mem_available_mib", lambda: 1 << 20)
    monkeypatch.setattr(sup, "find_queued", lambda gh, repo: found if repo == "o/ok" else [])
    state = sup.State()
    sup.tick(None, state, ["o/ok"], 0.0)  # type: ignore[arg-type]
    sup.tick(None, state, ["o/ok"], 20.0)  # type: ignore[arg-type]
    assert started == []
    assert lines == ["job 7 in o/ok has waited 0m00s here: no VM has its labels (homelab-fc, gpu)"]
    # The alert names the repo and labels, so nobody has to search the journal for them.
    assert 'homelab_ci_wait_seconds{kind="other",repo="o/ok",labels="homelab-fc,gpu"} 20' in (
        wait_metrics.read_text().splitlines()
    )


def test_an_unreadable_pressure_file_does_not_hold_vms(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sup, "IO_PRESSURE", tmp_path / "missing")
    assert READ_DISK_WAIT() == 0.0


def test_a_held_job_in_a_repo_not_polled_this_time_stays_held(host) -> None:
    queue, _, _ = host
    queue(jobs=0, free_mib=1 << 20)
    state = sup.State(held={5: "o/quiet"})
    sup.tick(None, state, ["o/ok"], 0.0)  # type: ignore[arg-type]
    assert state.held == {5: "o/quiet"}


def test_a_standby_gives_way_to_a_job_waiting_for_memory(host, monkeypatch) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 1)
    stopped: list[str] = []
    monkeypatch.setattr(
        sup, "stop_if_still_idle", lambda gh, run, why: stopped.append(f"{run.repo} {why}")
    )
    standby = _launch(0, True)  # waiting ready for octo/agent, holding 4 GiB
    state = sup.State(launches={0: standby})
    queue(jobs=1, free_mib=3072 + 1024)  # a free slot, but not the memory for the job
    sup.tick(None, state, ["o/ok", "octo/agent"], 0.0)  # type: ignore[arg-type]
    assert started == []  # neither the job nor another standby
    assert state.held == {0: "o/ok"}
    assert stopped == ["octo/agent is in the way of a queued job"]


def test_no_standby_boots_while_a_job_waits_for_memory(host, monkeypatch) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 1)
    queue(jobs=0, free_mib=1 << 20)
    state = sup.State(held={5: "o/quiet"})
    sup.tick(None, state, ["o/ok"], 0.0)  # type: ignore[arg-type]
    assert started == []
    state.held = {}
    sup.tick(None, state, ["o/ok"], 2.0)  # type: ignore[arg-type]
    assert started == [("o/ok", 4096, True)]


def test_a_standby_boots_only_with_memory_for_it(host, monkeypatch) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 1)
    queue(jobs=0, free_mib=3072 + 4095)
    sup.tick(None, sup.State(), ["o/ok"], 0.0)  # type: ignore[arg-type]
    assert started == []


def test_the_polled_repos_are_asked_at_once(monkeypatch) -> None:
    repos = ["o/a", "o/b", "o/c", "o/d"]
    # Each answer waits until every repo is being asked: one after another, it would time out.
    together = threading.Barrier(len(repos), timeout=5)

    def find(gh, repo: str) -> list[sup.Job]:
        together.wait()
        return [sup.Job(repo, 1, "check", tuple(sup.RUNNER_LABELS))]

    monkeypatch.setattr(sup, "check_private", lambda *a, **k: True)
    monkeypatch.setattr(sup, "only_colin", lambda *a, **k: True)
    monkeypatch.setattr(sup, "find_queued", find)
    monkeypatch.setattr(sup, "reap", lambda *a, **k: None)
    monkeypatch.setattr(sup, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    launched: list[str] = []
    monkeypatch.setattr(
        sup, "launch", lambda gh, state, repo, slot, now, **k: launched.append(repo)
    )
    spent = sup.tick(None, sup.State(slots=5), repos, 0.0)  # type: ignore[arg-type]
    assert launched == repos  # in the order given, whatever order the answers came in
    assert list(spent) == repos


class Connection:
    """api.github.com as seen down one connection, recording what is sent; `drop` makes
    its next request fail as if GitHub had closed it while it sat idle."""

    made: ClassVar[list[Connection]] = []

    def __init__(self, host: str, timeout: float) -> None:
        self.sent: list[str] = []
        self.drop = False
        Connection.made.append(self)

    def request(self, method: str, path: str, body: bytes | None, headers: dict) -> None:
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


def test_requests_share_one_open_connection_and_a_closed_one_is_reopened(monkeypatch) -> None:
    monkeypatch.setattr(Connection, "made", [])
    monkeypatch.setattr(sup.http.client, "HTTPSConnection", Connection)
    gh = sup.GitHub("t")
    gh.request("GET", "/a")
    gh.request("GET", "/b")
    assert len(Connection.made) == 1 and Connection.made[0].sent == ["GET /a", "GET /b"]
    Connection.made[0].drop = True
    gh.request("GET", "/c")
    assert len(Connection.made) == 2 and Connection.made[1].sent == ["GET /c"]
    # A registration is never sent twice, so it never goes down a connection that may be closed.
    gh.request("POST", "/r", {})
    assert len(Connection.made) == 3 and Connection.made[2].sent == ["POST /r"]


def test_a_standby_does_not_make_its_repo_polled_every_time_a_job_does(
    monkeypatch, tmp_path
) -> None:
    class Registers:
        def request(self, method: str, path: str, body: object = None) -> dict:
            return {"runner": {"id": 1}, "encoded_jit_config": "x"}

    monkeypatch.setattr(sup, "STATE_DIR", tmp_path)
    monkeypatch.setattr(sup, "LAST_REPO_FILE", tmp_path / "last-repo")
    state = sup.State()
    sup.launch(Registers(), state, "o/spare", 0, 0.0, cache=None, standby=True)  # type: ignore[arg-type]
    assert state.used == set()
    # Once the standby takes a job, its repo counts.
    monkeypatch.setattr(sup, "is_idle", lambda gh, run: False)
    sup.reap(None, state, 60.0, {}, ["o/spare"])  # type: ignore[arg-type]
    assert state.used == {"o/spare"}
    sup.launch(Registers(), state, "o/job", 1, 0.0, cache=None)  # type: ignore[arg-type]
    assert state.used == {"o/spare", "o/job"}


@pytest.fixture
def pools(caches, monkeypatch):
    """Build jobs queued in one repo; launch records (slot, label)."""
    monkeypatch.setattr(sup, "check_private", lambda *a, **k: True)
    monkeypatch.setattr(sup, "only_colin", lambda *a, **k: True)
    monkeypatch.setattr(sup, "reap", lambda *a, **k: None)
    monkeypatch.setattr(sup, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    started: list[tuple[int, str]] = []

    def launch(gh, state, repo, slot, now, *, cache, label="", standby=False, jobs=()) -> None:
        started.append((slot, label))
        state.launches[slot] = sup.Launch(
            slot, repo, slot, f"fc-{slot}", now, None, standby, label=label
        )  # type: ignore[arg-type]

    monkeypatch.setattr(sup, "launch", launch)

    def queue(build: int) -> None:
        jobs = [sup.Job("o/ok", n, "b", tuple(sup.RUNNER_LABELS)) for n in range(build)]
        monkeypatch.setattr(sup, "find_queued", lambda gh, repo: jobs)

    return queue, started


def test_the_standby_after_a_labelled_job_registers_with_its_label_and_disk(
    caches, monkeypatch
) -> None:
    registered: list[list[str]] = []
    disks: list[str] = []

    class Registers:
        def request(self, method: str, path: str, body: dict | None = None) -> dict:
            if method == "GET":  # the Builder's VM has its job
                return {"busy": True}
            assert body is not None
            registered.append(body["labels"])
            return {"runner": {"id": len(registered)}, "encoded_jit_config": "x"}

    def start_vm(run, timeout, env) -> None:
        disks.append(env["CACHE_IMAGE"])

    for name in ("check_private", "only_colin"):
        monkeypatch.setattr(sup, name, lambda *a, **k: True)
    monkeypatch.setattr(sup, "reap", lambda *a, **k: None)
    monkeypatch.setattr(sup, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(sup, "standby_count", lambda: 1)
    monkeypatch.setattr(sup, "STATE_DIR", caches)
    monkeypatch.setattr(sup, "LAST_REPO_FILE", caches / "last-repo")
    monkeypatch.setattr(sup, "start_vm", start_vm)
    stopped: list[str] = []
    monkeypatch.setattr(sup, "stop_if_still_idle", lambda gh, run, why: stopped.append(why))
    labelled = (*sup.RUNNER_LABELS, "fc-cache-octo--widgets")
    jobs: list[sup.Job] = []
    monkeypatch.setattr(sup, "find_queued", lambda gh, repo: jobs if repo == "octo/agent" else [])
    repos = ["octo/agent", "octo/widgets"]
    state = sup.State(slots=2)

    # A task's Builder asks for another repo's cache disk: its VM and the next standby both
    # register with that label and get that disk.
    jobs[:] = [sup.Job("octo/agent", 1, "build", labelled)]
    sup.tick(Registers(), state, repos, 0.0)  # type: ignore[arg-type]
    assert registered == [[*labelled], [*labelled]]
    assert disks == [str(caches / "octo__widgets.ext4")] * 2
    assert [run.standby for run in state.launches.values()] == [False, True]
    assert sup.recalled_last_job() == ("octo/agent", "fc-cache-octo--widgets")

    # Its Reviewer, asking for the same, is left to the standby: no VM boots, none is stopped.
    jobs[:] = [sup.Job("octo/agent", 2, "review", labelled)]
    sup.tick(Registers(), state, repos, 100.0)  # type: ignore[arg-type]
    assert len(registered) == 2 and stopped == []

    # A job wanting something else, with no free slot, still gets the standby out of its way.
    jobs[:] = [sup.Job("octo/agent", 3, "check", tuple(sup.RUNNER_LABELS))]
    sup.tick(Registers(), state, repos, 200.0)  # type: ignore[arg-type]
    assert stopped == ["is in the way of a queued job"]


def test_a_builders_reviewer_takes_a_standby_booted_when_its_builders_vm_ended(
    caches, monkeypatch
) -> None:
    registered: list[list[str]] = []

    class Registers:
        def request(self, method: str, path: str, body: dict | None = None) -> dict:
            if method == "GET":  # every runner has its job, or is still connecting
                return {"busy": True}
            assert body is not None
            registered.append(body["labels"])
            return {"runner": {"id": len(registered)}, "encoded_jit_config": "x"}

    (caches / "octo__gears.ext4").touch()
    for name in ("check_private", "only_colin"):
        monkeypatch.setattr(sup, name, lambda *a, **k: True)
    monkeypatch.setattr(sup, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(sup, "delete_runner", lambda *a, **k: True)
    monkeypatch.setattr(sup, "standby_count", lambda: 1)
    monkeypatch.setattr(sup, "STATE_DIR", caches)
    monkeypatch.setattr(sup, "LAST_REPO_FILE", caches / "last-repo")
    stopped: list[str] = []
    monkeypatch.setattr(sup, "stop_if_still_idle", lambda gh, run, why: stopped.append(why))
    widgets = (*sup.RUNNER_LABELS, "fc-cache-octo--widgets")
    jobs: list[sup.Job] = []
    monkeypatch.setattr(sup, "find_queued", lambda gh, repo: jobs if repo == "octo/agent" else [])
    repos = ["octo/agent", "octo/widgets", "octo/gears"]
    # Task A's Builder runs on widgets' disk; task B's, started after it, on gears'.
    builder = sup.Launch(0, "octo/agent", 1, "fc-0-1", 0.0, caches / "m0", label=widgets[-1])
    other = sup.Launch(
        1, "octo/agent", 2, "fc-1-1", 10.0, caches / "m1", label="fc-cache-octo--gears"
    )
    state = sup.State(slots=2, launches={0: builder, 1: other})
    state.last_repo, state.last_label = "octo/agent", other.label
    # A's Builder ends before GitHub queues its Reviewer: the standby boots in its slot for
    # widgets, not for gears, the last job started.
    monkeypatch.setattr(
        sup, "running_vms", lambda: {r.unit for r in state.launches.values()} - {builder.unit}
    )
    sup.tick(Registers(), state, repos, 600.0)  # type: ignore[arg-type]
    assert registered == [[*widgets]] and state.launches[0].standby
    assert sup.recalled_last_job() == ("octo/agent", "fc-cache-octo--widgets")

    # The Reviewer, queued on the next poll, is left to that standby: no VM boots for it and
    # none is stopped.
    jobs[:] = [sup.Job("octo/agent", 2, "review", widgets)]
    sup.tick(Registers(), state, repos, 602.0)  # type: ignore[arg-type]
    assert len(registered) == 1 and "is in the way of a queued job" not in stopped


def test_a_reviewer_takes_a_standby_booted_while_its_builder_ran(caches, monkeypatch) -> None:
    registered: list[list[str]] = []

    class Registers:
        def request(self, method: str, path: str, body: dict | None = None) -> dict:
            if method == "GET":
                return {"busy": True}
            assert body is not None
            registered.append(body["labels"])
            return {"runner": {"id": len(registered)}, "encoded_jit_config": "x"}

    for name in ("check_private", "only_colin"):
        monkeypatch.setattr(sup, name, lambda *a, **k: True)
    monkeypatch.setattr(sup, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(sup, "delete_runner", lambda *a, **k: True)
    monkeypatch.setattr(sup, "standby_count", lambda: 2)
    monkeypatch.setattr(sup, "STATE_DIR", caches)
    monkeypatch.setattr(sup, "LAST_REPO_FILE", caches / "last-repo")
    stopped: list[str] = []
    monkeypatch.setattr(sup, "stop_if_still_idle", lambda gh, run, why: stopped.append(why))
    widgets = (*sup.RUNNER_LABELS, "fc-cache-octo--widgets")
    jobs: list[sup.Job] = []
    monkeypatch.setattr(sup, "find_queued", lambda gh, repo: jobs if repo == "octo/agent" else [])
    repos = ["octo/agent"]
    # Task A's Builder runs on widgets' disk; task B's build, started after it, on gears'.
    builder = sup.Launch(0, "octo/agent", 1, "fc-0-1", 0.0, caches / "m0", label=widgets[-1])
    other = sup.Launch(
        1, "octo/agent", 2, "fc-1-1", 10.0, caches / "m1", label="fc-cache-octo--gears"
    )
    state = sup.State(slots=3, launches={0: builder, 1: other})
    state.last_repo, state.last_label = "octo/agent", other.label
    monkeypatch.setattr(sup, "running_vms", lambda: {r.unit for r in state.launches.values()})
    # While both builds run, the free slot's standby boots for A's, the older, not B's, the last
    # started.
    sup.tick(Registers(), state, repos, 20.0)  # type: ignore[arg-type]
    assert registered == [[*widgets]] and state.launches[2].standby

    # A's Builder ends and its Reviewer queues: the booted standby takes it, nothing boots
    # for it and nothing is stopped; the freed slot's standby goes to B's label.
    monkeypatch.setattr(
        sup, "running_vms", lambda: {r.unit for r in state.launches.values()} - {builder.unit}
    )
    jobs[:] = [sup.Job("octo/agent", 2, "review", widgets)]
    sup.tick(Registers(), state, repos, 22.0)  # type: ignore[arg-type]
    assert registered == [[*widgets], [*sup.RUNNER_LABELS, "fc-cache-octo--gears"]]
    assert all(run.standby for run in state.launches.values() if run.slot != 1)
    assert stopped == []


def test_a_second_standby_boots_only_with_a_spare_slot_and_memory(host, monkeypatch) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 2)
    standby = _launch(0, True)
    standby.repo = "o/ok"
    running = _launch(1, False)
    running.repo, running.label = "o/ok", "fc-cache-o--x"
    queue(jobs=0, free_mib=1 << 20)
    state = sup.State(slots=2, launches={0: standby, 1: running})
    sup.tick(None, state, ["o/ok"], 0.0)  # type: ignore[arg-type]
    assert started == []  # no free slot
    state.slots = 3
    queue(jobs=0, free_mib=3072 + 4095)
    sup.tick(None, state, ["o/ok"], 2.0)  # type: ignore[arg-type]
    assert started == []  # no memory above the reserve
    queue(jobs=0, free_mib=3072 + 4096)
    sup.tick(None, state, ["o/ok"], 4.0)  # type: ignore[arg-type]
    assert started == [("o/ok", 4096, True)]
    sup.tick(None, state, ["o/ok"], 6.0)  # type: ignore[arg-type]
    assert len(started) == 1  # two wait already


@pytest.mark.parametrize(("count", "boots"), [(2, 2), (1, 1)])
def test_a_second_standby_boots_for_the_same_repo_and_label(
    host, monkeypatch, count: int, boots: int
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: count)
    queue(jobs=0, free_mib=1 << 20)
    state = sup.State(slots=2)
    for now in (0.0, 2.0, 4.0):
        sup.tick(None, state, ["o/ok"], now)  # type: ignore[arg-type]
    assert started == [("o/ok", 4096, True)] * boots


def test_each_wanted_repo_gets_a_standby_before_any_gets_a_second(host, monkeypatch) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 2)
    running = _launch(0, False)
    running.repo = "o/b"
    queue(jobs=0, free_mib=1 << 20)
    state = sup.State(slots=4, launches={0: running}, last_repo="o/a")
    for now in (0.0, 2.0, 4.0):
        sup.tick(None, state, ["o/a", "o/b"], now)  # type: ignore[arg-type]
    assert [(repo, standby) for repo, _, standby in started] == [("o/a", True), ("o/b", True)]


def test_a_second_standby_for_one_repo_gives_way_to_a_newly_wanted_one(host, monkeypatch) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 2)
    stopped: list[int] = []
    monkeypatch.setattr(sup, "stop_if_still_idle", lambda gh, run, why: stopped.append(run.slot))
    first, second, running = _launch(0, True), _launch(1, True), _launch(2, False)
    first.repo, second.repo, running.repo = "o/a", "o/a", "o/b"
    second.started = 1.0
    queue(jobs=0, free_mib=1 << 20)
    state = sup.State(slots=4, launches={0: first, 1: second, 2: running}, last_repo="o/a")
    sup.tick(None, state, ["o/a", "o/b"], 2.0)  # type: ignore[arg-type]
    assert stopped == [1]
    assert started == []  # o/b's boots once the stopped one has gone


def test_a_build_standby_gives_way_to_another_labels_job_with_no_free_slot(
    host, monkeypatch
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 2)
    stopped: list[str] = []
    monkeypatch.setattr(
        sup, "stop_if_still_idle", lambda gh, run, why: stopped.append(f"{run.repo} {why}")
    )
    standby = _launch(0, True)
    standby.label = "fc-cache-o--x"
    queue(jobs=1, free_mib=1 << 20)
    state = sup.State(slots=1, launches={0: standby})
    sup.tick(None, state, ["o/ok", "octo/agent"], 0.0)  # type: ignore[arg-type]
    assert stopped == ["octo/agent is in the way of a queued job"]
    assert started == []  # the job takes the slot once the standby has gone


@pytest.mark.parametrize("busy", [True, False])
def test_a_starting_vm_covers_a_queued_job_only_until_its_runner_takes_one(
    pools, monkeypatch, busy
) -> None:
    queue, started = pools
    queue(build=1)
    label, slot = "", 0
    asked: list[str] = []

    def has_job(gh, run) -> bool:
        asked.append(run.runner_name)
        return busy

    monkeypatch.setattr(sup, "has_job", has_job)
    state = sup.State(slots=2)
    # Started a minute ago for the same repo and label.
    state.launches[slot] = sup.Launch(
        slot,
        "o/ok",
        slot,
        f"fc-{slot}",
        0.0,
        None,  # type: ignore[arg-type]
        label=label,
    )
    sup.tick(None, state, ["o/ok"], 60.0)  # type: ignore[arg-type]
    if busy:  # it has its own job, so the queued one gets a VM of its own
        assert len(started) == 1 and started[0][1] == label and state.launches[slot].busy
    else:  # its runner is still connecting and will take the queued job: no duplicate
        assert started == []
    assert asked == [f"fc-{slot}"]


# The real one, before the fixture below replaces it in every test.
START_VM = sup.start_vm


class EveryUnit(set):
    """Every VM unit counts as running, unless a test says otherwise."""

    def __contains__(self, item: object) -> bool:
        return True


@pytest.fixture(autouse=True)
def vms(tmp_path, monkeypatch):
    """VM records in a temporary folder, VMs never really started, and all still running."""
    monkeypatch.setattr(sup, "VMS_DIR", tmp_path / "vms")
    monkeypatch.setattr(sup, "start_vm", lambda run, timeout, env: None)
    monkeypatch.setattr(sup, "running_vms", lambda: EveryUnit())
    return tmp_path / "vms"


def test_a_restarted_supervisor_takes_back_running_vms_and_drops_ended_ones(
    vms, monkeypatch
) -> None:
    running = sup.Launch(3, "o/ok", 7, "fc-3-100", 100.0, Path("m3"), label="fc-cache-o--ok")
    ended = sup.Launch(4, "o/ok", 8, "fc-4-100", 100.0, Path("m4"))
    for run in (running, ended):
        sup.write_status(run.record, sup.record(run))
    stopped: list[list[str]] = []
    monkeypatch.setattr(sup.subprocess, "run", lambda cmd, **k: stopped.append(cmd))
    units = {running.unit, "homelab-ci-vm-fc-9-1"}  # the second has no record
    monkeypatch.setattr(sup, "running_vms", lambda: units)

    state = sup.State()
    sup.adopt(state)

    assert state.launches == {3: running}
    assert not ended.record.exists()
    assert stopped == [["systemctl", "stop", "--no-block", "homelab-ci-vm-fc-9-1"]]


def test_a_vm_that_ended_is_forgotten_and_its_runner_removed(vms, monkeypatch) -> None:
    run = sup.Launch(0, "o/ok", 7, "fc-0-1", 0.0, vms / "metadata")
    sup.write_status(run.record, sup.record(run))
    removed: list[str] = []
    monkeypatch.setattr(sup, "delete_runner", lambda gh, repo, rid, name: removed.append(name))
    monkeypatch.setattr(sup, "running_vms", lambda: set())
    state = sup.State(launches={0: run})

    sup.reap(None, state, 10.0)  # type: ignore[arg-type]

    assert state.launches == {} and removed == ["fc-0-1"] and not run.record.exists()


def test_an_ended_vms_failed_bookkeeping_frees_its_slot_and_the_queued_job_still_starts(
    vms, monkeypatch
) -> None:
    ended = sup.Launch(0, "o/ok", 7, "fc-0-1", 0.0, vms / "metadata")
    sup.write_status(ended.record, sup.record(ended))

    def count_memory(run: sup.Launch) -> None:
        raise KeyError("deploy")

    monkeypatch.setattr(sup, "count_memory", count_memory)
    removed: list[str] = []
    monkeypatch.setattr(sup, "delete_runner", lambda gh, repo, rid, name: removed.append(name))
    monkeypatch.setattr(sup, "running_vms", lambda: set())
    for name in ("check_private", "only_colin"):
        monkeypatch.setattr(sup, name, lambda *a, **k: True)
    monkeypatch.setattr(sup, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(sup, "has_job", lambda gh, run: False)
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    job = sup.Job("o/ok", 1, "j", tuple(sup.RUNNER_LABELS))
    monkeypatch.setattr(sup, "find_queued", lambda gh, repo: [job] if repo == "o/ok" else [])
    started: list[int] = []

    def launch(gh, state, repo, slot, now, **k) -> None:
        started.append(slot)
        state.launches[slot] = _launch(slot, False)

    monkeypatch.setattr(sup, "launch", launch)
    lines: list[str] = []
    monkeypatch.setattr(sup, "log", lines.append)
    state = sup.State(slots=1, launches={0: ended})

    sup.tick(None, state, ["o/ok"], 10.0)  # type: ignore[arg-type]

    assert started == [0] and state.launches[0] is not ended
    assert removed == ["fc-0-1"]  # the rest of its bookkeeping still ran
    assert "slot 0: fc-0-1 bookkeeping failed: KeyError: 'deploy'" in lines


def test_reaping_waits_while_systemd_cannot_be_asked(monkeypatch) -> None:
    monkeypatch.setattr(sup, "running_vms", lambda: None)
    run = sup.Launch(0, "o/ok", 7, "fc-0-1", 0.0, Path("m"))
    state = sup.State(launches={0: run})
    sup.reap(None, state, 10.0)  # type: ignore[arg-type]
    assert state.launches == {0: run}


def test_stale_runners_skip_the_vms_still_running() -> None:
    class Runners:
        deleted: ClassVar[list[str]] = []

        def request(self, method: str, path: str, body: object = None) -> dict:
            if method == "DELETE":
                Runners.deleted.append(path.rsplit("/", 1)[1])
                return {}
            return {"runners": [{"id": 1, "name": "fc-1-5"}, {"id": 2, "name": "fc-2-5"}]}

        def forget(self, drop) -> None:
            pass

    sup.remove_stale_runners(Runners(), ["o/ok"], keep=["fc-2-5"])  # type: ignore[arg-type]
    assert Runners.deleted == ["1"]


def test_a_standby_that_took_a_job_is_recorded_as_a_job_vm(vms, monkeypatch) -> None:
    run = sup.Launch(0, "o/ok", 7, "fc-0-1", 0.0, Path("m"), True)
    sup.write_status(run.record, sup.record(run))
    monkeypatch.setattr(sup, "is_idle", lambda gh, r: False)
    state = sup.State(launches={0: run})
    sup.reap(None, state, 60.0, {}, ["o/ok"])  # type: ignore[arg-type]
    assert json.loads(run.record.read_text())["standby"] is False
    assert "o/ok" in state.used  # a job keeps its repo polled


def test_the_console_reader_keeps_only_exact_memory_lines() -> None:
    seen: list[dict[str, int]] = []
    console = io.BytesIO(
        b"\nHOMELAB-MEM 1 812 1985 0\n"
        b"noise HOMELAB-MEM 1 9999 1985 0\n"
        b"HOMELAB-MEM 1 -5 1985 0\n"
        b"\nHOMELAB-MEM 1 990 1985 2\r\n"
    )
    sup.read_console(console, seen.append)
    assert seen == [
        {"peak": 812, "total": 1985, "kills": 0},
        {"peak": 990, "total": 1985, "kills": 2},
    ]


@pytest.fixture
def memory(monkeypatch, tmp_path):
    """Fresh counts, a temporary textfile, and the log lines."""
    # The supervisor's own kinds, so a kind it lacks fails here as it would on the host.
    monkeypatch.setattr(sup, "MEMORY_COUNTS", {k: [0, 0, 0] for k in sup.MEMORY_COUNTS})
    monkeypatch.setattr(sup, "MEMORY_METRICS", tmp_path / "ci-vm-memory.prom")
    lines: list[str] = []
    monkeypatch.setattr(sup, "log", lines.append)
    return lines


def _ended(vms: Path, label: str, peak: int, kills: int, standby: bool = False) -> sup.Launch:
    run = sup.Launch(6, "o/ok", 7, "fc-6-1", 0.0, vms / "m", standby, label=label)
    sup.write_status(run.memory_file, {"peak": peak, "total": 1000, "kills": kills})
    return run


def test_an_ended_vm_is_counted_by_kind_and_its_memory_file_removed(vms, memory) -> None:
    sup.write_memory_metrics()
    assert 'homelab_ci_vm_oom_kills_total{kind="build"} 0' in sup.MEMORY_METRICS.read_text()
    run = _ended(vms, "", peak=850, kills=1)
    sup.forget(run)
    text = sup.MEMORY_METRICS.read_text()
    assert 'homelab_ci_vms_ended_total{kind="build"} 1' in text
    assert 'homelab_ci_vms_near_full_total{kind="build"} 1' in text  # 850 of 1000 is over 80%
    assert 'homelab_ci_vm_oom_kills_total{kind="build"} 1' in text
    assert memory == [
        "slot 6: fc-6-1 (build) needed at least 850 MiB of 1000, 1 out-of-memory kills"
    ]
    assert not run.memory_file.exists()


def test_an_ended_vm_with_a_cache_label_is_counted(vms, memory) -> None:
    run = _ended(vms, "fc-cache-o--build", peak=300, kills=0)
    sup.forget(run)
    assert sup.MEMORY_COUNTS["build"][0] == 1


def test_a_standby_that_never_took_a_job_or_a_silent_vm_is_not_counted(vms, memory) -> None:
    sup.forget(_ended(vms, "", peak=300, kills=0, standby=True))
    sup.forget(sup.Launch(5, "o/ok", 7, "fc-5-1", 0.0, vms / "m"))  # no memory line came
    assert memory == [] and sup.MEMORY_COUNTS["build"] == [0, 0, 0]


def test_a_vm_is_told_how_long_its_queued_jobs_waited_and_why(monkeypatch, tmp_path) -> None:
    class Registers:
        def request(self, method: str, path: str, body: object = None) -> dict:
            return {"runner": {"id": 1}, "encoded_jit_config": "x"}

    for name in ("STATE_DIR", "OUTBOX_DIR"):
        monkeypatch.setattr(sup, name, tmp_path)
    monkeypatch.setattr(sup, "LAST_REPO_FILE", tmp_path / "last-repo")
    monkeypatch.setattr(sup, "start_vm", lambda run, timeout, env: None)
    monkeypatch.setattr(sup, "log", lambda line: None)
    state = sup.State()
    sup.note_waits(state, ["o/ok"], {7: ("o/ok", "all 2 build slots in use")}, 100.0)
    sup.launch(Registers(), state, "o/ok", 0, 340.0, cache=None, jobs=[9, 7])  # type: ignore[arg-type]
    metadata = json.loads((tmp_path / "metadata-slot0.json").read_text())
    assert metadata["waits"] == {
        "7": {"seconds": 240, "why": "all 2 build slots in use"},
        "9": {"seconds": 0, "why": ""},
    }
    monkeypatch.setattr(sup, "VM_WAITS_MAX", 1)
    sup.launch(Registers(), state, "o/ok", 1, 340.0, cache=None, jobs=[9, 7])  # type: ignore[arg-type]
    assert list(json.loads((tmp_path / "metadata-slot1.json").read_text())["waits"]) == ["7"]


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

    found = sup.queued_jobs("o/ok", [{"id": 1}, {"id": 2}], jobs.__getitem__)

    assert [(job.id, job.run_started) for job in found] == [(13, True), (22, False)]


@pytest.mark.parametrize(
    ("build_waited", "first"),
    [
        (60.0, "fc-cache-o--review"),  # the started run's review goes first
        (sup.FIRST_BUILD_WAIT_MAX + 60, "fc-cache-o--build"),  # the build waited too long
    ],
)
def test_a_started_runs_review_takes_the_slot_before_an_older_new_build(
    pools, monkeypatch, build_waited: float, first: str
) -> None:
    _, started = pools
    now = 10_000.0
    labels = tuple(sup.RUNNER_LABELS)
    jobs = [
        sup.Job("o/ok", 1, "build", (*labels, "fc-cache-o--build"), queued_at=now - build_waited),
        sup.Job("o/ok", 2, "review", (*labels, "fc-cache-o--review"), run_started=True),
    ]
    monkeypatch.setattr(sup, "find_queued", lambda gh, repo: jobs)

    sup.tick(None, sup.State(slots=1), ["o/ok"], now)  # type: ignore[arg-type]

    assert started == [(0, first)]
