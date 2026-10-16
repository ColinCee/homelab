import tarfile

import pytest

from scripts import ci_supervisor as sup


def test_reads_the_repo_list() -> None:
    text = "# private repos only\nColinCee/cortex\n\nColinCee/servo  # Windows app, Linux checks\n"

    assert sup.read_repos(text) == ["ColinCee/cortex", "ColinCee/servo"]


@pytest.mark.parametrize("line", ["cortex", "ColinCee/", "/cortex", "a/b/c"])
def test_a_malformed_repo_line_is_an_error(line: str) -> None:
    with pytest.raises(ValueError):
        sup.read_repos(line)


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
        ({"a/x": 1}, {}, 2, ["a/x"]),
        ({"a/x": 3}, {}, 2, ["a/x", "a/x"]),
        ({"a/x": 2}, {"a/x": 1}, 2, ["a/x"]),  # one VM is already booting for it
        ({"a/x": 1}, {"a/x": 1}, 2, []),
        ({"a/x": 1, "b/y": 1}, {}, 1, ["a/x"]),
        ({}, {}, 2, []),
        ({"a/x": 1}, {}, 0, []),
    ],
)
def test_plans_one_vm_per_uncovered_job_within_free_slots(
    queued: dict[str, int], booting: dict[str, int], free: int, starts: list[str]
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


@pytest.mark.parametrize(("running", "size"), [(0, (4, 4096)), (1, (2, 2048))])
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


def _artifact(
    id: int, branch: str, created: str, name: str = "homelab-fc-cache", expired: bool = False
) -> dict:
    return {
        "id": id,
        "name": name,
        "expired": expired,
        "created_at": created,
        "workflow_run": {"id": id * 10, "head_branch": branch},
    }


def test_takes_the_newest_cache_artifact_from_the_default_branch_only() -> None:
    artifacts = [
        _artifact(1, "master", "2026-10-15T10:00:00Z"),
        _artifact(2, "master", "2026-10-15T12:00:00Z"),
        _artifact(3, "evil-branch", "2026-10-15T13:00:00Z"),  # a branch cannot replace the cache
        _artifact(4, "master", "2026-10-15T14:00:00Z", expired=True),
        _artifact(5, "master", "2026-10-15T15:00:00Z", name="something-else"),
    ]

    picked = sup.pick_cache_artifact(artifacts, "master")

    assert picked is not None and picked["id"] == 2
    assert sup.pick_cache_artifact(artifacts, "main") is None


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


def test_the_cache_unpacker_refuses_an_archive_that_unpacks_too_big(tmp_path) -> None:
    check = sup.size_capped(100)
    small, big = tarfile.TarInfo("cache/a"), tarfile.TarInfo("cache/b")
    small.size, big.size = 60, 60

    check(small, str(tmp_path))
    with pytest.raises(tarfile.FilterError):
        check(big, str(tmp_path))


def test_keeps_as_many_standbys_as_asked_while_slots_are_free() -> None:
    assert sup.wants_standby(["a/x"], ["a/x"], [True], 1, wanted=2) is True
    assert sup.wants_standby(["a/x"], ["a/x"], [True, True], 0, wanted=2) is False
    assert sup.wants_standby(["a/x"], ["a/x"], [], 2, wanted=0) is False
