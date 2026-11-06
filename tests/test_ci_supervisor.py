import email.message
import http.client
import io
import json
import os
import tarfile
import threading
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
        (["ubuntu-latest"], False),
        (["homelab-fc", "gpu"], False),  # asks for something our VMs lack
        (["homelab-fc", "beelink"], False),  # the deploy runner's label
    ],
)
def test_takes_only_jobs_asking_for_our_label(labels: list[str], ours: bool) -> None:
    assert sup.wants_us(labels) is ours


def test_finds_queued_jobs_for_us_only() -> None:
    runs = [{"id": 1}, {"id": 2}]
    jobs = {
        1: [
            {"id": 11, "name": "check", "status": "queued", "labels": ["homelab-fc"]},
            {"id": 12, "name": "windows", "status": "queued", "labels": ["windows-latest"]},
        ],
        2: [{"id": 21, "name": "check", "status": "in_progress", "labels": ["homelab-fc"]}],
    }

    found = sup.queued_jobs("ColinCee/cortex", runs, jobs.__getitem__)

    assert [job.id for job in found] == [11]


@pytest.mark.parametrize(
    ("queued", "booting", "free", "starts"),
    [
        (["a/x"], {}, 2, ["a/x"]),
        (["a/x"] * 3, {}, 2, ["a/x", "a/x"]),
        (["a/x"] * 2, {"a/x": 1}, 2, ["a/x"]),  # one VM is already booting for it
        (["a/x"], {"a/x": 1}, 2, []),
        (["a/x", "b/y"], {}, 1, ["a/x"]),
        (["b/y", "a/x"], {}, 1, ["b/y"]),  # the oldest job first, whatever its name
        (["a/x", "b/y", "a/x"], {"a/x": 1}, 1, ["b/y"]),  # a booting VM covers the oldest
        ([], {}, 2, []),
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
        ({"full_name": "ColinCee/cortex"}, False),  # missing means no
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


@pytest.mark.parametrize(("running", "size"), [(0, (4, 4096)), (1, (2, 2048)), (2, (2, 2048))])
def test_a_job_alone_gets_all_four_cores(running: int, size: tuple[int, int]) -> None:
    assert sup.vm_size(running) == size


@pytest.mark.parametrize(
    ("repos", "private", "standbys", "free", "wanted"),
    [
        (["a/x", "b/y"], ["a/x", "b/y"], [], 2, True),
        (["a/x"], ["a/x"], [False], 1, True),  # a job VM running: still keep one waiting
        (["a/x"], ["a/x"], [True], 1, False),  # one is already waiting
        (["a/x"], ["a/x"], [False, False], 0, False),  # no free slot
        (["a/x"], [], [], 2, False),  # no longer private: nothing kept for it
        ([], [], [], 2, False),
    ],
)
def test_keeps_one_standby_vm_for_the_first_private_repo(
    repos: list[str], private: list[str], standbys: list[bool], free: int, wanted: bool
) -> None:
    assert sup.wants_standby(repos, private, standbys, free) is wanted


@pytest.mark.parametrize("kind", [tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE])
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
    assert json.loads((caches / "octo__app.json").read_text()) == {"run": 70, "sha": "abc"}


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
        b"\0" * 64,
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


def test_a_cache_tar_that_fails_to_unpack_is_dropped(outbox_dir, caches) -> None:
    tar = _outbox(outbox_dir, GREEN)
    tar.write_bytes(GREEN.ljust(512, b"\0") + b"not a tar")

    assert sup.take_outbox(Runs(), tar, {"octo/app": "main"}, 0.0) is False
    assert not sup.cache_image("octo/app").exists()


def test_keeps_as_many_standbys_as_asked_while_slots_are_free() -> None:
    assert sup.wants_standby(["a/x"], ["a/x"], [True], 1, wanted=2) is True
    assert sup.wants_standby(["a/x"], ["a/x"], [True, True], 0, wanted=2) is False
    assert sup.wants_standby(["a/x"], ["a/x"], [], 2, wanted=0) is False


@pytest.mark.parametrize(
    ("label", "repo"),
    [
        ("fc-cache-octo--widgets", "octo/widgets"),
        ("FC-Cache-Octo--Widgets", "octo/widgets"),  # GitHub matches labels in any case
        ("fc-cache-octo--my--app", "octo/my--app"),  # an owner name cannot hold "--"
        ("fc-cache-octo-widgets", None),
        ("fc-cache---widgets", None),
        ("fc-cache-octo--", None),
        ("homelab-fc", None),
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
        (["homelab-fc", "fc-cache-octo--widgets", "gpu"], False),
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


@pytest.mark.parametrize(
    "private",
    [
        ["octo/agent"],  # not served, or served but public: not in this poll's private list
        ["octo/agent", "octo/nodisk"],
    ],
)
def test_a_cache_label_naming_an_unserved_or_public_repo_gets_no_disk(
    caches, private: list[str]
) -> None:
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
        "light_slots": 0,
        "light_in_use": 0,
        "light_standby": 0,
        "cache_disks": ["octo/agent", "octo/widgets"],
        "github_ok": True,
        "mem_available_mib": 9000,
        "held_for_memory": 1,
    }


@pytest.mark.parametrize(
    ("text", "slots"),
    [(None, 5), ("", 5), ("four\n", 5), ("-2\n", 5), ("0\n", 1), ("3\n", 3), (" 2 ", 2)],
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


LIVE = b"KAIZEN-LIVE 1 105 builder 1792999990 runs the tests (uv run pytest -q)\r\n"


def test_only_lines_in_the_exact_live_format_are_kept() -> None:
    assert sup.live_line(LIVE, 1792999998.7) == {
        "task": 105,
        "who": "builder",
        "at": 1792999990,
        "heard": 1792999998,
        "text": "runs the tests (uv run pytest -q)",
    }
    for noise in [
        b"[    0.412] EXT4-fs (vda): mounted filesystem\n",
        b"KAIZEN-LIVE 2 105 builder 1792999990 a later format\n",
        b"KAIZEN-LIVE 1 105 judge 1792999990 not an agent with a line\n",
        b"KAIZEN-LIVE 1 105 builder 179299999 a nine-digit time\n",
        b"KAIZEN-LIVE 1 105 builder 1792999990 \n",
        b"KAIZEN-LIVE 1 105 builder 1792999990 " + b"x" * 121 + b"\n",
        b"KAIZEN-LIVE 1 105 builder 1792999990 tab\there\n",
        b"KAIZEN-LIVE 1 105 builder 1792999990 \xff not text\n",
        b"boot KAIZEN-LIVE 1 105 builder 1792999990 not at the start\n",
    ]:
        assert sup.live_line(noise, 0) is None, noise


def test_the_console_reader_keeps_the_last_valid_line_and_drops_over_long_ones() -> None:
    later = b"KAIZEN-LIVE 1 105 builder 1793000005 edits src/kaizen/live.py\n"
    over_long = b"KAIZEN-LIVE 1 105 builder 1793000009 " + b"x" * 5000 + b"\n"
    console = io.BytesIO(b"Linux version 6.1\n" + LIVE + later + over_long + b"login:")
    kept: list[dict] = []
    sup.read_console(console, kept.append)
    assert [line["at"] for line in kept] == [1792999990, 1793000005]
    assert console.closed


EVENT = b'{"kind":"text","at":1792999990,"text":"Running the tests"}'


def test_session_events_are_saved_in_order_beside_live_lines(tmp_path) -> None:
    session = tmp_path / "sessions" / "fc-0-1.jsonl"
    console = io.BytesIO(
        b"KAIZEN-SESSION 1 "
        + EVENT
        + b"\r\n"
        + LIVE
        + b"KAIZEN-SESSION 1 not json\n"
        + b'KAIZEN-SESSION 1 {"text":"caf\xc3\xa9"}\n'  # not ASCII
        + b'KAIZEN-SESSION 1 {"a":1}\n'
    )
    kept: list[dict] = []
    sup.read_console(console, kept.append, session)

    assert session.read_bytes() == EVENT + b'\n{"a":1}\n'
    assert len(kept) == 1


def test_a_session_file_stops_growing_at_its_cap(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sup, "SESSION_MAX_BYTES", len(EVENT) * 2)
    session = tmp_path / "s.jsonl"
    sup.read_console(io.BytesIO((b"KAIZEN-SESSION 1 " + EVENT + b"\n") * 3), None, session)
    assert session.read_bytes() == EVENT + b"\n"


def test_a_failed_save_never_stops_the_console_reader(tmp_path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("")
    console = io.BytesIO(b"KAIZEN-SESSION 1 " + EVENT + b"\n" + LIVE)
    kept: list[dict] = []
    sup.read_console(console, kept.append, blocked / "s.jsonl")  # its folder is a file
    assert len(kept) == 1
    assert console.closed


def test_a_deeply_nested_line_is_dropped_and_reading_goes_on(tmp_path) -> None:
    # Deep enough for Python 3.12's JSON parser, the Beelink's, to give up with RecursionError.
    deep = b"KAIZEN-SESSION 1 {" + b'"a":' + b"[" * 2000 + b"]" * 2000 + b"}" + bytes([10])
    session = tmp_path / "s.jsonl"
    kept: list[dict] = []
    sup.read_console(io.BytesIO(deep + LIVE), kept.append, session)
    assert len(kept) == 1


def test_session_files_go_after_thirty_days(tmp_path) -> None:
    old, new = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
    for path in (old, new):
        path.write_text("{}\n")
    os.utime(old, (0, 1_000_000))
    sup.prune_sessions(tmp_path, 1_000_000 + sup.SESSION_KEEP_SECONDS + 1)
    assert [p.name for p in tmp_path.iterdir()] == ["new.jsonl"]


def test_only_auto_kaizen_vms_get_a_live_line(monkeypatch) -> None:
    started: list[list[str]] = []
    monkeypatch.setattr(sup.subprocess, "run", lambda cmd, **k: started.append(cmd))
    ours = sup.Launch(0, "ColinCee/auto-kaizen", 1, "fc-0", 0.0, Path("m0"))
    product = sup.Launch(1, "ColinCee/servo", 2, "fc-1", 0.0, Path("m1"))
    for run in (ours, product):
        START_VM(run, 60, {})
    assert "--live" in started[0] and "--live" not in started[1]

    # Its console reader keeps the latest line in its live file; the supervisor reads it.
    line = sup.live_line(LIVE, 1793000000.0)
    sup.write_status(ours.live_file, line)
    state = sup.State(launches={1: product, 0: ours})
    assert sup.live(state, 1793000000.2) == {"updated": 1793000000, "lines": [line]}


def test_the_live_file_is_written_beside_the_status_file(tmp_path) -> None:
    path = tmp_path / "live.json"
    sup.write_status(path, sup.live(sup.State(), 5.0))
    assert json.loads(path.read_text()) == {"updated": 5, "lines": []}
    assert path.stat().st_mode & 0o777 == 0o644
    assert sup.LIVE_FILE.parent == sup.STATUS_FILE.parent


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

    def launch(gh, state, repo, slot, now, *, cache, label="", standby=False) -> None:
        mem = sup.next_size(state, standby)[1]
        started.append((repo, mem, standby))
        run = _launch(slot, standby)
        run.repo = repo
        state.launches[slot] = run

    monkeypatch.setattr(sup, "launch", launch)

    def queue(jobs: int, free_mib: int) -> None:
        monkeypatch.setattr(
            sup,
            "find_queued",
            lambda gh, repo: (
                [sup.Job(repo, n, "j", tuple(sup.RUNNER_LABELS)) for n in range(jobs)]
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
    assert [line for line in lines if "memory" in line] == ["job 1 in o/ok waits for memory"]
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
    assert lines[-1] == "job 0 in o/ok waits for a busy disk"

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
    assert "job 1 in o/ok waits for a busy disk" in lines
    sup.tick(None, state, ["o/ok"], 30.0)  # type: ignore[arg-type]
    assert len(started) == 1  # the first VM's load has not shown yet
    sup.tick(None, state, ["o/ok"], 61.0)  # type: ignore[arg-type]
    assert len(started) == 2


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


LIGHT = (*sup.LIGHT_RUNNER_LABELS,)


@pytest.mark.parametrize(
    ("labels", "ours"),
    [
        (LIGHT, True),
        (("self-hosted", "linux", "x64", "HOMELAB-FC-LIGHT"), True),
        ((*LIGHT, "homelab-fc"), False),  # a build VM never has the light label
        ((*LIGHT, "fc-cache-colincee--servo"), False),  # a light VM has no cache disk
    ],
)
def test_a_light_job_asks_for_the_light_label_alone(labels, ours) -> None:
    assert sup.wants_us(labels) is ours


def test_a_light_jobs_want_is_the_light_label() -> None:
    assert sup.Job("o/ok", 1, "j", LIGHT).want == ("o/ok", sup.LIGHT_LABEL)


@pytest.fixture
def pools(caches, monkeypatch):
    """Build and light jobs queued in one repo; launch records (slot, label)."""
    monkeypatch.setattr(sup, "check_private", lambda *a, **k: True)
    monkeypatch.setattr(sup, "only_colin", lambda *a, **k: True)
    monkeypatch.setattr(sup, "reap", lambda *a, **k: None)
    monkeypatch.setattr(sup, "maybe_take_outboxes", lambda *a, **k: None)
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    started: list[tuple[int, str]] = []

    def launch(gh, state, repo, slot, now, *, cache, label="", standby=False) -> None:
        started.append((slot, label))
        state.launches[slot] = sup.Launch(
            slot, repo, slot, f"fc-{slot}", now, None, standby, label=label
        )  # type: ignore[arg-type]

    monkeypatch.setattr(sup, "launch", launch)

    def queue(build: int, light: int) -> None:
        jobs = [sup.Job("o/ok", n, "b", tuple(sup.RUNNER_LABELS)) for n in range(build)]
        jobs += [sup.Job("o/ok", 100 + n, "l", LIGHT) for n in range(light)]
        monkeypatch.setattr(sup, "find_queued", lambda gh, repo: jobs)

    return queue, started


def test_light_jobs_start_only_in_light_slots_and_build_jobs_only_in_build_slots(pools) -> None:
    queue, started = pools
    queue(build=7, light=4)
    sup.tick(None, sup.State(slots=5, light_slots=3), ["o/ok"], 0.0)  # type: ignore[arg-type]
    light = [(slot, sup.LIGHT_LABEL) for slot in (5, 6, 7)]
    assert sorted(started) == [(0, ""), (1, ""), (2, ""), (3, ""), (4, ""), *light]


def test_light_jobs_still_start_while_every_build_slot_is_busy(pools) -> None:
    queue, started = pools
    queue(build=0, light=2)
    state = sup.State(slots=2, light_slots=2)
    for slot in (0, 1):
        state.launches[slot] = _launch(slot, False)
    sup.tick(None, state, ["o/ok"], 0.0)  # type: ignore[arg-type]
    assert started == [(2, sup.LIGHT_LABEL), (3, sup.LIGHT_LABEL)]


def test_no_light_slots_means_light_jobs_wait(pools) -> None:
    queue, started = pools
    queue(build=1, light=2)
    sup.tick(None, sup.State(slots=5, light_slots=0), ["o/ok"], 0.0)  # type: ignore[arg-type]
    assert started == [(0, "")]


def test_light_vms_do_not_shrink_a_build_vm() -> None:
    state = sup.State(slots=5, light_slots=2)
    state.launches[5] = sup.Launch(5, "o/ok", 5, "fc-5", 0.0, None, label=sup.LIGHT_LABEL)  # type: ignore[arg-type]
    assert sup.next_size(state, False) == (4, 4096)


def test_the_status_file_counts_light_vms_apart() -> None:
    state = sup.State(slots=5, light_slots=5, launches={0: _launch(0, False)})
    state.launches[5] = sup.Launch(5, "o/ok", 5, "fc-5", 0.0, None, label=sup.LIGHT_LABEL)  # type: ignore[arg-type]
    status = sup.status(state, 0.0, github_ok=True, mem_available=9000)
    assert (status["in_use"], status["light_slots"], status["light_in_use"]) == (1, 5, 1)


@pytest.mark.parametrize(("text", "slots"), [(None, 0), (" 0 ", 0), ("3", 3), ("lots", 0)])
def test_the_light_slot_count_comes_from_the_host_file(tmp_path, monkeypatch, text, slots) -> None:
    path = tmp_path / "light-slots"
    if text is not None:
        path.write_text(text)
    monkeypatch.setattr(sup, "LIGHT_SLOTS_FILE", path)
    assert sup.light_slot_count() == slots


def test_an_old_last_repo_file_with_the_repo_alone_still_loads(monkeypatch, tmp_path) -> None:
    path = tmp_path / "last-repo"
    path.write_text("a/y\n")
    monkeypatch.setattr(sup, "LAST_REPO_FILE", path)
    assert sup.recalled_last_job() == ("a/y", "")


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


@pytest.mark.parametrize(("label", "slot"), [("", 0), (sup.LIGHT_LABEL, 2)])
@pytest.mark.parametrize("busy", [True, False])
def test_a_starting_vm_covers_a_queued_job_only_until_its_runner_takes_one(
    pools, monkeypatch, label, slot, busy
) -> None:
    queue, started = pools
    queue(build=0 if label else 1, light=1 if label else 0)
    asked: list[str] = []

    def has_job(gh, run) -> bool:
        asked.append(run.runner_name)
        return busy

    monkeypatch.setattr(sup, "has_job", has_job)
    state = sup.State(slots=2, light_slots=2)
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


def test_a_free_light_slot_gets_a_ready_light_vm_for_the_last_light_jobs_repo(
    pools, monkeypatch
) -> None:
    queue, started = pools
    queue(build=0, light=1)
    state = sup.State(slots=2, light_slots=2)
    sup.tick(None, state, ["o/ok"], 0.0)  # type: ignore[arg-type]
    state.last_light_repo = "o/ok"  # as launch remembers it
    assert started == [(2, sup.LIGHT_LABEL)]  # the job; one slot left free

    queue(build=0, light=0)
    sup.tick(None, state, ["o/ok"], 2.0)  # type: ignore[arg-type]
    assert started[-1] == (3, sup.LIGHT_LABEL) and state.launches[3].standby
    status = sup.status(state, 2.0, github_ok=True, mem_available=9000)
    assert (status["in_use"], status["standby"]) == (0, 0)
    assert (status["light_in_use"], status["light_standby"]) == (1, 1)

    sup.tick(None, state, ["o/ok"], 4.0)  # type: ignore[arg-type]
    assert len(started) == 2  # one is enough

    monkeypatch.setattr(sup, "light_standby_count", lambda: 0)
    del state.launches[3]
    sup.tick(None, state, ["o/ok"], 6.0)  # type: ignore[arg-type]
    assert len(started) == 2


def test_a_light_job_of_its_repo_takes_the_light_standby(pools) -> None:
    queue, started = pools
    queue(build=0, light=1)
    state = sup.State(slots=1, light_slots=1, last_light_repo="o/ok")
    state.launches[1] = sup.Launch(1, "o/ok", 1, "fc-1", 0.0, None, True, label=sup.LIGHT_LABEL)  # type: ignore[arg-type]
    sup.tick(None, state, ["o/ok"], 60.0)  # type: ignore[arg-type]
    assert started == []  # left to the ready VM


def test_the_light_standby_gives_way_to_another_repos_light_job(pools, monkeypatch) -> None:
    queue, started = pools
    queue(build=0, light=1)
    stopped: list[str] = []
    monkeypatch.setattr(
        sup, "stop_if_still_idle", lambda gh, run, why: stopped.append(f"{run.repo} {why}")
    )
    state = sup.State(slots=1, light_slots=1, last_light_repo="o/other")
    state.launches[1] = sup.Launch(1, "o/other", 1, "fc-1", 0.0, None, True, label=sup.LIGHT_LABEL)  # type: ignore[arg-type]
    sup.tick(None, state, ["o/ok", "o/other"], 60.0)  # type: ignore[arg-type]
    assert stopped == ["o/other is in the way of a queued job"]
    assert started == []  # the job takes the slot once the standby has gone


def test_the_light_standby_stays_out_of_a_build_jobs_way_for_a_slot(pools, monkeypatch) -> None:
    queue, _ = pools
    queue(build=1, light=0)
    stopped: list[str] = []
    monkeypatch.setattr(sup, "stop_if_still_idle", lambda gh, run, why: stopped.append(why))
    state = sup.State(slots=1, light_slots=1, last_light_repo="o/ok")
    state.launches[0] = _launch(0, False)  # the build slot is busy
    state.launches[1] = sup.Launch(1, "o/ok", 1, "fc-1", 0.0, None, True, label=sup.LIGHT_LABEL)  # type: ignore[arg-type]
    sup.tick(None, state, ["o/ok"], 60.0)  # type: ignore[arg-type]
    assert stopped == []  # a build cannot use a light slot


def test_the_light_standby_gives_its_memory_to_a_build_and_never_boots_while_one_waits(
    host, monkeypatch
) -> None:
    queue, started, _ = host
    monkeypatch.setattr(sup, "standby_count", lambda: 0)
    stopped: list[str] = []
    monkeypatch.setattr(sup, "stop_if_still_idle", lambda gh, run, why: stopped.append(why))
    state = sup.State(slots=2, light_slots=2, last_light_repo="o/ok")
    state.launches[2] = sup.Launch(2, "o/ok", 2, "fc-2", 0.0, None, True, label=sup.LIGHT_LABEL)  # type: ignore[arg-type]
    queue(jobs=1, free_mib=3072 + 1024)  # a free build slot, but not the memory
    sup.tick(None, state, ["o/ok"], 0.0)  # type: ignore[arg-type]
    assert stopped == ["is in the way of a job waiting for memory"]
    assert started == []  # nor another light standby in the free light slot


def test_the_last_light_jobs_repo_is_kept_and_polled_every_time(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sup, "LAST_LIGHT_REPO_FILE", tmp_path / "last-light-repo")
    monkeypatch.setattr(sup, "CACHE_DIR", tmp_path)
    assert sup.recalled_last_light_repo() == ""
    sup.remember(sup.LAST_LIGHT_REPO_FILE, "a/k\n")
    assert sup.recalled_last_light_repo() == "a/k"
    state = sup.State(last_light_repo="a/k", polled={"a/k": 0.0, "a/q": 0.0})
    assert sup.due(["a/k", "a/q"], state, 30.0) == ["a/k"]


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


@pytest.mark.parametrize("label", ["", sup.LIGHT_LABEL])
def test_a_standby_that_took_a_job_is_recorded_as_a_job_vm(vms, monkeypatch, label) -> None:
    run = sup.Launch(0, "o/ok", 7, "fc-0-1", 0.0, Path("m"), True, label=label)
    sup.write_status(run.record, sup.record(run))
    monkeypatch.setattr(sup, "is_idle", lambda gh, r: False)
    state = sup.State(launches={0: run})
    sup.reap(None, state, 60.0, {}, ["o/ok"])  # type: ignore[arg-type]
    assert json.loads(run.record.read_text())["standby"] is False
    assert ("o/ok" in state.used) is (label == "")  # only a build job keeps its repo polled


def test_no_light_standby_boots_while_the_disk_is_busy(pools, monkeypatch) -> None:
    queue, started = pools
    queue(build=0, light=0)
    monkeypatch.setattr(sup, "disk_wait_percent", lambda: 90.0)
    state = sup.State(slots=1, light_slots=1, last_light_repo="o/ok")
    sup.tick(None, state, ["o/ok"], 0.0)  # type: ignore[arg-type]
    assert started == []
    monkeypatch.setattr(sup, "disk_wait_percent", lambda: 0.0)
    sup.tick(None, state, ["o/ok"], 2.0)  # type: ignore[arg-type]
    assert started == [(1, sup.LIGHT_LABEL)]
