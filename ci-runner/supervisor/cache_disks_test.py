import email.message
import io
import json
import tarfile
import urllib.error
from pathlib import Path
from typing import IO, Any

import cache_disks
import ci_supervisor as sup
import github_api
import pytest
import vms

from conftest import NoGitHub, linux_only


@pytest.fixture(autouse=True, name="outbox_dir")
def fixture_outbox_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    path = tmp_path_factory.mktemp("outbox")
    monkeypatch.setattr(cache_disks, "OUTBOX_DIR", path)
    return path


def test_a_public_repos_job_never_gets_or_gives_another_repos_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache_disks, "CACHE_DIR", tmp_path)
    (tmp_path / "ColinCee__cortex.ext4").touch()
    private = ["ColinCee/cortex", "ColinCee/servo"]
    assert cache_disks.cache_for("ColinCee/servo", "fc-cache-ColinCee--cortex", private) is not None
    assert cache_disks.cache_for("ColinCee/homelab", "fc-cache-ColinCee--cortex", private) is None


@pytest.mark.parametrize("kind", [tarfile.CHRTYPE, tarfile.FIFOTYPE])
def test_the_cache_unpacker_refuses_device_files(kind: bytes, tmp_path: Path) -> None:
    member = tarfile.TarInfo("cache/dev")
    member.type = kind

    with pytest.raises(tarfile.FilterError):
        cache_disks.safe_member(member, str(tmp_path))


def test_the_cache_unpacker_refuses_paths_outside_its_folder(tmp_path: Path) -> None:
    member = tarfile.TarInfo("../../etc/cron.d/x")

    with pytest.raises(tarfile.FilterError):
        cache_disks.safe_member(member, str(tmp_path))


def test_the_cache_unpacker_refuses_a_hard_link_out_through_a_symlink(tmp_path: Path) -> None:
    linux_only("symlinks, which Windows lets only admins make")
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "victim").write_text("host file")
    (root / "a").symlink_to("../outside")  # as if unpacked from the same archive
    link = tarfile.TarInfo("h")
    link.type, link.linkname = tarfile.LNKTYPE, "a/victim"

    with pytest.raises(tarfile.FilterError):
        cache_disks.safe_member(link, str(root))


def test_the_cache_unpacker_refuses_an_archive_that_unpacks_too_big(tmp_path: Path) -> None:
    check = cache_disks.size_capped(100)
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


def test_the_cache_unpacker_takes_the_later_entry_of_a_path_held_twice(tmp_path: Path) -> None:
    # servo's cache, 5 Nov: web/node_modules twice, the second time with a hard link onto
    # a file the first had unpacked, and tarfile tried to seek back in the stream.
    archive = _tar(
        ("web/esbuild/bin/esbuild", b"js shim"),
        ("web/linux-x64/esbuild", b"binary"),
        ("web/esbuild/bin/esbuild", "web/linux-x64/esbuild"),
    )

    cache_disks.unpack_cache(io.BytesIO(archive), tmp_path)

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


class Runs(NoGitHub):
    """GitHub's answers about run 70, attempt 1."""

    def __init__(
        self, event: str = "push", branch: str = "main", job: dict[str, Any] | None = None
    ) -> None:
        self.run = {"id": 70, "event": event, "head_branch": branch, "head_sha": "abc"}
        default = {"runner_name": RUNNER, "status": "completed", "conclusion": "success"}
        self.jobs = [{"runner_name": "other", "status": "completed", "conclusion": "success"}]
        self.jobs.append(default if job is None else job)
        self.asked: list[str] = []

    def request(self, method: str, path: str, body: object = None) -> dict[str, Any]:
        self.asked.append(path)
        if path.endswith("/jobs?per_page=100"):
            assert path.startswith("/repos/octo/app/actions/runs/70/attempts/1/")
            return {"jobs": self.jobs}
        assert path == "/repos/octo/app/actions/runs/70/attempts/1"
        return self.run


GREEN = b"homelab-ci-cache v1 run=70 attempt=1\n"


@pytest.fixture
def built(caches: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """What each cache disk build would have held."""
    names: list[list[str]] = []

    def build(stream: IO[bytes], image: Path) -> None:
        with tarfile.open(fileobj=stream, mode="r|") as tar:
            names.append([m.name for m in tar])
        image.touch()

    monkeypatch.setattr(cache_disks, "build_cache_image", build)
    return names


def test_builds_the_cache_disk_from_a_green_default_branch_jobs_outbox(
    outbox_dir: Path, built: list[list[str]], caches: Path
) -> None:
    tar = _outbox(outbox_dir, GREEN)

    assert cache_disks.take_outbox(Runs(), tar, {"octo/app": "main"}, 0.0) is False  # done with it

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
    github: Runs, outbox_dir: Path, built: list[list[str]]
) -> None:
    tar = _outbox(outbox_dir, GREEN)

    assert cache_disks.take_outbox(github, tar, {"octo/app": "main"}, 0.0) is False
    assert built == []


def test_keeps_an_outbox_until_github_shows_its_job_finished(
    outbox_dir: Path, built: list[list[str]]
) -> None:
    tar = _outbox(outbox_dir, GREEN)
    running = Runs(job={"runner_name": RUNNER, "status": "in_progress", "conclusion": None})
    made = tar.stat().st_mtime

    assert cache_disks.take_outbox(running, tar, {"octo/app": "main"}, made + 60) is True
    assert cache_disks.take_outbox(running, tar, {"octo/app": "main"}, made + 3601) is False
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
    header: bytes, outbox_dir: Path, built: list[list[str]]
) -> None:
    github = Runs()

    assert (
        cache_disks.take_outbox(github, _outbox(outbox_dir, header), {"octo/app": "main"}, 0.0)
        is False
    )
    assert github.asked == [] and built == []


def test_an_outbox_older_than_the_disks_run_is_not_taken(
    outbox_dir: Path, built: list[list[str]], caches: Path
) -> None:
    (caches / "octo__app.json").write_text(json.dumps({"run": 71, "sha": "def"}))

    assert (
        cache_disks.take_outbox(Runs(), _outbox(outbox_dir, GREEN), {"octo/app": "main"}, 0.0)
        is False
    )
    assert built == []


def test_an_outbox_of_a_repo_no_longer_served_is_not_taken(
    outbox_dir: Path, built: list[list[str]]
) -> None:
    github = Runs()

    assert cache_disks.take_outbox(github, _outbox(outbox_dir, GREEN), {}, 0.0) is False
    assert github.asked == [] and built == []


def test_judges_only_the_outboxes_of_vms_that_ended(outbox_dir: Path) -> None:
    for name in ("fc-0-1", "fc-1-2", "fc-2-3"):
        (outbox_dir / f"{name}.json").write_text("{}")
    state = sup.State()
    state.launches[0] = vms.Launch(0, "o/r", 1, "fc-0-1", 0.0, Path("m"))
    state.cache_checked["fc-2-3"] = 100.0  # tried 30 s ago

    assert cache_disks.ended_outboxes(state, 130.0) == [outbox_dir / "fc-1-2.tar"]


class Failing(NoGitHub):
    def __init__(self, error: Exception) -> None:
        self.error = error

    def request(self, method: str, path: str, body: object = None) -> dict[str, Any]:
        raise self.error


@pytest.mark.parametrize(
    ("error", "kept"),
    [
        (urllib.error.HTTPError("u", 404, "Not Found", email.message.Message(), None), False),
        (urllib.error.HTTPError("u", 502, "Bad Gateway", email.message.Message(), None), True),
        (github_api.RateLimited(0.0), True),
    ],
)
def test_keeps_an_outbox_only_while_github_may_answer_later(
    error: Exception, kept: bool, outbox_dir: Path, built: list[list[str]]
) -> None:
    tar = _outbox(outbox_dir, GREEN)

    assert (
        cache_disks.judge_outbox(Failing(error), tar, {"octo/app": "main"}, tar.stat().st_mtime)
        is kept
    )
    assert tar.exists() is kept and tar.with_suffix(".json").exists() is kept


def test_a_cache_tar_that_fails_to_unpack_is_dropped(outbox_dir: Path, caches: Path) -> None:
    tar = _outbox(outbox_dir, GREEN)
    tar.write_bytes(GREEN.ljust(512, b"\0") + b"not a tar")

    assert cache_disks.take_outbox(Runs(), tar, {"octo/app": "main"}, 0.0) is False
    assert not cache_disks.cache_image("octo/app").exists()


@pytest.fixture(name="caches")
def fixture_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cache_disks, "CACHE_DIR", tmp_path)
    (tmp_path / "octo__widgets.ext4").touch()
    return tmp_path


def test_a_job_without_a_cache_label_gets_its_own_repos_disk(caches: Path) -> None:
    assert cache_disks.cache_for("octo/agent", "", ["octo/agent"]) == caches / "octo__agent.ext4"


def test_a_cache_label_naming_a_served_private_repo_gets_its_disk(caches: Path) -> None:
    disk = cache_disks.cache_for(
        "octo/agent", "FC-CACHE-octo--widgets", ["octo/agent", "octo/widgets"]
    )

    assert disk == caches / "octo__widgets.ext4"


def test_a_cache_label_naming_an_unserved_or_public_repo_gets_no_disk(caches: Path) -> None:
    # widgets has a disk but is not in this poll's private list; nodisk is, but has no disk.
    private = ["octo/agent", "octo/nodisk"]
    assert cache_disks.cache_for("octo/agent", "fc-cache-octo--widgets", private) is None
    assert cache_disks.cache_for("octo/agent", "fc-cache-octo--nodisk", private) is None
