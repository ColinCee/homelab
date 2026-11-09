"""Handoffs: files a job hands to later jobs through the Beelink (.github/actions/handoff and
ci_supervisor.py's take_handoffs and answer_ask)."""

import io
import json
import os
import re
import subprocess
import tarfile
import threading
import time
from pathlib import Path

import pytest
import yaml

from scripts import ci_supervisor as sup

ACTION = Path(__file__).parent.parent / ".github" / "actions" / "handoff"
REPO = "octo/app"
BUILDER, REVIEWER, LANDER = "fc-1-1", "fc-2-2", "fc-9-3"


@pytest.fixture(autouse=True)
def folders(tmp_path_factory, monkeypatch) -> Path:
    monkeypatch.setattr(sup, "OUTBOX_DIR", tmp_path_factory.mktemp("outbox"))
    handoffs = tmp_path_factory.mktemp("handoff")
    monkeypatch.setattr(sup, "HANDOFF_DIR", handoffs)
    (handoffs / "octo__app").mkdir()
    return handoffs


class GitHub:
    """Run 70: attempt 1 ran `build` on BUILDER (done) and runs `review` on REVIEWER."""

    def __init__(self, build_status: str = "completed") -> None:
        self.jobs = {
            70: [
                {
                    "name": "build",
                    "runner_name": BUILDER,
                    "status": build_status,
                    "conclusion": "success",
                },
                {
                    "name": "review",
                    "runner_name": REVIEWER,
                    "status": "in_progress",
                    "conclusion": None,
                },
            ],
            71: [
                {"name": "land", "runner_name": LANDER, "status": "in_progress", "conclusion": None}
            ],
        }
        for jobs in self.jobs.values():
            for job in jobs:
                job["run_attempt"] = 1
        # Run 72 is a pull request's.
        self.runs = {70: ("issues", "main"), 71: ("issues", "main"), 72: ("pull_request", "x")}
        self.asked: list[str] = []

    def request(self, method: str, path: str, body: object = None) -> dict:
        self.asked.append(path)
        if path == f"/repos/{REPO}":
            return {"default_branch": "main"}
        prefix = f"/repos/{REPO}/actions/runs/"
        assert path.startswith(prefix)
        run = int(path.removeprefix(prefix).split("/")[0])
        if "/jobs" not in path:
            event, branch = self.runs[run]
            return {"event": event, "head_branch": branch}
        return {"jobs": self.jobs.get(run, [])}


def run_script(*args: str, tmp: Path, **env: str) -> subprocess.CompletedProcess[str]:
    full = {
        **os.environ,
        "SUDO": "",
        "HANDOFF_DIRECT": "",
        "RUNNER_TEMP": str(tmp),
        "GITHUB_RUN_ID": "70",
        "GITHUB_RUN_ATTEMPT": "1",
        **env,
    }
    return subprocess.run(
        ["bash", str(ACTION / "handoff.sh"), *args],
        capture_output=True,
        text=True,
        env=full,
        timeout=60,
    )


def outbox_of(runner: str, tmp: Path, puts: list[tuple[str, str, str]]) -> Path:
    """The outbox a VM leaves after the action put each (item, readers, later)."""
    tar = sup.OUTBOX_DIR / f"{runner}.tar"
    tar.write_bytes(b"homelab-ci-outbox\n".ljust(4 << 20, b"\0"))
    (sup.OUTBOX_DIR / f"{runner}.json").write_text(json.dumps({"repo": REPO}))
    for item, readers, later in puts:
        files = tmp / item
        files.mkdir(parents=True, exist_ok=True)
        (files / "verdict.json").write_text(f'{{"item": "{item}"}}')
        result = run_script("put", item, str(files), readers, later, "90", tmp=tmp, DISKS=str(tar))
        assert result.returncode == 0, result.stderr
    return tar


def test_a_put_is_filed_under_the_job_github_shows_for_that_vm(tmp_path, folders) -> None:
    tar = outbox_of(BUILDER, tmp_path, [("builder", "review land", "line"), ("ask", "land", "")])

    assert sup.judge_outbox(GitHub(), tar, {}, 0.0) is False

    job = folders / "octo__app" / "70" / "build"
    assert sorted(p.name for p in job.iterdir()) == [
        "ask.json",
        "ask.tar",
        "builder.json",
        "builder.tar",
    ]
    meta = json.loads((job / "builder.json").read_text())
    assert (meta["readers"], meta["later"], meta["attempt"]) == (["review", "land"], ["line"], 1)
    assert not tar.exists() and not tar.with_suffix(".json").exists()


def test_an_outbox_is_kept_until_github_shows_its_job_finished(tmp_path, folders) -> None:
    tar = outbox_of(BUILDER, tmp_path, [("builder", "review", "")])

    assert sup.judge_outbox(GitHub("in_progress"), tar, {}, tar.stat().st_mtime) is True
    assert not (folders / "octo__app" / "70").exists()


def test_a_vm_naming_a_run_it_did_not_run_files_nothing(tmp_path, folders) -> None:
    tar = outbox_of("fc-5-5", tmp_path, [("review", "land", "")])  # a Builder's VM posing

    assert sup.judge_outbox(GitHub(), tar, {}, 0.0) is False
    assert not (folders / "octo__app" / "70").exists()


@pytest.mark.parametrize("name", ["../evil.tar", "a/b.tar", "UPPER.tar", ".hidden.tar"])
def test_items_with_bad_names_are_skipped(name: str, tmp_path, folders) -> None:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as t:
        for member in (name, name.removesuffix(".tar") + ".json"):
            info = tarfile.TarInfo(member)
            info.size = 2
            t.addfile(info, io.BytesIO(b"{}"))
    tar = sup.OUTBOX_DIR / f"{BUILDER}.tar"
    tar.write_bytes(
        b"homelab-ci-handoff v1 run=70 attempt=1\n".ljust(512, b"\0") + payload.getvalue()
    )
    (sup.OUTBOX_DIR / f"{BUILDER}.json").write_text(json.dumps({"repo": REPO}))

    assert sup.judge_outbox(GitHub(), tar, {}, 0.0) is False
    assert list((folders / "octo__app" / "70" / "build").glob("*")) == []
    assert not (folders / "evil.tar").exists()


def test_a_repo_past_its_limit_drops_new_handoffs(tmp_path, folders, monkeypatch) -> None:
    monkeypatch.setattr(sup, "HANDOFF_REPO_MAX_BYTES", 100)
    tar = outbox_of(BUILDER, tmp_path, [("builder", "review", "")])

    sup.judge_outbox(GitHub(), tar, {}, 0.0)

    assert not (folders / "octo__app" / "70" / "build" / "builder.tar").exists()


def file_item(
    folders: Path, run: int, job: str, item: str, readers: list[str], later: list[str]
) -> None:
    folder = folders / "octo__app" / str(run) / job
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{item}.tar").write_bytes(b"payload")
    meta = {"attempt": 1, "readers": readers, "later": later, "expires": 10**10}
    (folder / f"{item}.json").write_text(json.dumps(meta))


def ask(own_run: int, run: int, job: str, item: str, wait: int = 0) -> re.Match[bytes]:
    line = f"HOMELAB-HANDOFF 1 abcdef12 {own_run} 1 {run} {job} {item} {wait}\n".encode()
    match = sup.HANDOFF_ASK.fullmatch(line)
    assert match
    return match


def answer(runner: str, match, github: GitHub | None = None, **kw) -> tuple[str, bytes]:
    inbox = sup.OUTBOX_DIR / f"{runner}.inbox"
    inbox.write_bytes(sup.INBOX_EMPTY.ljust(1 << 20, b"\0"))
    verdict = sup.answer_ask(github or GitHub(), REPO, runner, inbox, match, {}, **kw)
    return verdict, inbox.read_bytes()


def test_a_reader_of_the_same_run_gets_the_item(folders) -> None:
    file_item(folders, 70, "build", "builder", ["review"], [])

    verdict, inbox = answer(REVIEWER, ask(70, 70, "build", "builder"))

    assert verdict == "ok"
    assert inbox.startswith(b"homelab-ci-inbox v1 abcdef12 ok 7\n")
    assert inbox[512:519] == b"payload"


@pytest.mark.parametrize(
    ("runner", "own_run", "readers", "later"),
    [
        (REVIEWER, 70, ["land"], []),  # review is not a reader
        (LANDER, 71, ["land"], []),  # a reader of run 70 only, asking from run 71
        (BUILDER, 70, ["build"], []),  # BUILDER's job has finished: it runs nothing now
    ],
)
def test_anyone_else_is_denied(runner, own_run, readers, later, folders) -> None:
    file_item(folders, 70, "line", "resume-review", readers, later)

    verdict, inbox = answer(runner, ask(own_run, 70, "line", "resume-review"))

    assert verdict == "denied"
    assert inbox.startswith(b"homelab-ci-inbox v1 abcdef12 denied 0\n")


def test_a_later_reader_gets_it_from_another_run(folders) -> None:
    file_item(folders, 70, "build", "builder", ["review"], ["land"])

    assert answer(LANDER, ask(71, 70, "build", "builder"))[0] == "ok"


def test_none_at_once_when_the_writer_finished_and_left_nothing(folders) -> None:
    github = GitHub()

    started = time.monotonic()
    verdict, _ = answer(REVIEWER, ask(70, 70, "build", "builder", wait=600), github)

    assert verdict == "none" and time.monotonic() - started < 5


def test_waits_for_an_outbox_still_to_be_judged(folders) -> None:
    (sup.OUTBOX_DIR / f"{BUILDER}.json").write_text("{}")  # its VM ended, not judged yet
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds
        if clock[0] >= 20:
            file_item(folders, 70, "build", "builder", ["review"], [])

    verdict, _ = answer(
        REVIEWER, ask(70, 70, "build", "builder", wait=600), clock=lambda: clock[0], sleep=sleep
    )

    assert verdict == "ok" and clock[0] >= 20


def test_the_console_reader_hands_asks_on_and_keeps_live_lines() -> None:
    asked: list[bytes] = []
    console = io.BytesIO(b"boot\nHOMELAB-HANDOFF 1 abcdef12 70 1 70 build builder 600\r\nmore\n")

    sup.read_console(console, None, None, lambda m: asked.append(m[6]))

    assert asked == [b"builder"]


def test_sweeps_expired_handoffs(folders) -> None:
    file_item(folders, 70, "build", "builder", [], [])
    meta = folders / "octo__app" / "70" / "build" / "builder.json"
    meta.write_text(json.dumps({"expires": 5}))

    sup.sweep_handoffs(10.0)

    assert not (folders / "octo__app" / "70").exists()


def test_get_reads_the_answer_off_the_inbox(tmp_path, folders) -> None:
    """The action's get, against the host's answer_ask on a console and inbox that are files."""
    file_item(folders, 70, "build", "builder", ["review"], [])
    folder = folders / "octo__app" / "70" / "build"
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "review.json").write_text('{"ok": true}')
    subprocess.run(["tar", "-C", str(payload), "-cf", str(folder / "builder.tar"), "."], check=True)
    inbox = sup.OUTBOX_DIR / f"{REVIEWER}.inbox"
    inbox.write_bytes(sup.INBOX_EMPTY.ljust(4 << 20, b"\0"))
    console = tmp_path / "ttyS0"
    console.write_text("")

    def host() -> None:
        for _ in range(200):
            match = sup.HANDOFF_ASK.fullmatch(console.read_bytes())
            if match:
                sup.answer_ask(GitHub(), REPO, REVIEWER, inbox, match, {})
                return
            time.sleep(0.05)

    threading.Thread(target=host, daemon=True).start()
    result = run_script(
        "get",
        "builder",
        "build",
        "70",
        str(tmp_path / "in"),
        "30",
        tmp=tmp_path,
        DISKS=f"{tmp_path / 'vdb'} {inbox}",
        HANDOFF_CONSOLE=str(console),
        GITHUB_RUN_ID="70",
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert (tmp_path / "in" / "review.json").read_text() == '{"ok": true}'


def test_prepare_reads_disks_with_gnu_dd_where_the_image_has_it(tmp_path) -> None:
    """Ubuntu 26.04's own dd (uutils) fails every direct read, so prepare found no disk."""
    shim = tmp_path / "bin"
    shim.mkdir()
    calls = tmp_path / "gnudd-calls"
    (shim / "gnudd").write_text(f'#!/bin/sh\necho "$@" >> {calls}\nexec dd "$@"\n')
    (shim / "gnudd").chmod(0o755)
    inbox = tmp_path / "inbox"
    inbox.write_bytes(sup.INBOX_EMPTY.ljust(4096, b"\0"))
    console = tmp_path / "ttyS0"
    console.write_text("")

    result = run_script(
        "prepare",
        tmp=tmp_path,
        DISKS=str(inbox),
        HANDOFF_CONSOLE=str(console),
        PATH=f"{shim}{os.pathsep}{os.environ['PATH']}",
    )

    assert result.returncode == 0, result.stderr
    assert f"if={inbox}" in calls.read_text()
    assert "no inbox disk" not in result.stdout
    assert "no outbox disk found" in result.stdout


def test_the_action_never_uploads_to_github() -> None:
    action = yaml.safe_load((ACTION / "action.yml").read_text())
    assert all("uses" not in step for step in action["runs"]["steps"])


def test_a_pull_requests_run_never_reads_other_runs(folders) -> None:
    github = GitHub()
    github.jobs[72] = [{"name": "land", "runner_name": "fc-8-8", "status": "in_progress"}]
    file_item(folders, 70, "build", "builder", [], ["land"])

    assert answer("fc-8-8", ask(72, 70, "build", "builder"), github)[0] == "denied"


def test_a_vm_asking_as_another_run_is_denied_without_asking_github(folders) -> None:
    github = GitHub()
    inbox = sup.OUTBOX_DIR / f"{REVIEWER}.inbox"
    inbox.write_bytes(sup.INBOX_EMPTY.ljust(1 << 20, b"\0"))
    known: dict = {}
    sup.answer_ask(github, REPO, REVIEWER, inbox, ask(70, 70, "build", "x"), known)
    asked = len(github.asked)

    assert (
        sup.answer_ask(github, REPO, REVIEWER, inbox, ask(71, 71, "land", "x"), known) == "denied"
    )
    assert len(github.asked) == asked


def test_a_rerun_waits_for_its_own_attempts_item(folders) -> None:
    github = GitHub()
    github.jobs[70][0]["run_attempt"] = 2  # build ran again; its VM's outbox is not filed yet
    (sup.OUTBOX_DIR / f"{BUILDER}.json").write_text("{}")
    file_item(folders, 70, "build", "builder", ["review"], [])  # attempt 1's
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds
        if clock[0] >= 20:
            meta = folders / "octo__app" / "70" / "build" / "builder.json"
            meta.write_text(json.dumps({"attempt": 2, "readers": ["review"]}))
            (sup.OUTBOX_DIR / f"{BUILDER}.json").unlink()

    verdict, _ = answer(
        REVIEWER, ask(70, 70, "build", "builder", 600), github, clock=lambda: clock[0], sleep=sleep
    )

    assert verdict == "ok" and clock[0] >= 20


def test_a_sparse_member_is_never_unpacked(tmp_path, folders) -> None:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w", format=tarfile.GNU_FORMAT) as t:
        info = tarfile.TarInfo("big.tar")
        info.type = tarfile.GNUTYPE_SPARSE
        info.size = 0
        t.addfile(info)
    assert sup.unpack_handoffs(io.BytesIO(payload.getvalue()), tmp_path, 10) == {}
    assert list(tmp_path.iterdir()) == []


def test_items_past_the_repos_limit_are_dropped(tmp_path) -> None:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as t:
        info = tarfile.TarInfo("builder.tar")
        info.size = 100
        t.addfile(info, io.BytesIO(b"x" * 100))
    with pytest.raises(sup.BadCache):
        sup.unpack_handoffs(io.BytesIO(payload.getvalue()), tmp_path, 50)


def test_a_light_vms_outbox_never_replaces_the_cache(tmp_path) -> None:
    tar = sup.OUTBOX_DIR / "fc-9-9.tar"
    tar.write_bytes(b"homelab-ci-cache v1 run=70 attempt=1\n".ljust(1024, b"\0"))
    (sup.OUTBOX_DIR / "fc-9-9.json").write_text(json.dumps({"repo": REPO, "light": True}))

    assert sup.take_outbox(GitHub(), tar, {REPO: "main"}, 0.0) is False
