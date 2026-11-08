"""The PC's build slots: the Beelink's door (peer_*) and the PC's side (PeerStream, pc mode)."""

import io
import json
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import ci_supervisor as sup
from tests.test_handoff import GitHub as Runs
from tests.test_handoff import file_item

REPO = "octo/app"
PC_REVIEWER = "pc-2-1793990000"


@pytest.fixture(autouse=True)
def folders(tmp_path, monkeypatch) -> Path:
    for name in ("OUTBOX_DIR", "PEER_DIR", "SESSIONS_DIR", "CACHE_DIR", "HANDOFF_DIR"):
        (tmp_path / name).mkdir()
        monkeypatch.setattr(sup, name, tmp_path / name)
    (tmp_path / "HANDOFF_DIR" / "octo__app").mkdir()
    slots = tmp_path / "peer-slots-max"
    slots.write_text("3\n")
    monkeypatch.setattr(sup, "PEER_SLOTS_FILE", slots)
    # The door and image fetch want tens of GiB free; a small test machine has less.
    monkeypatch.setattr(sup.shutil, "disk_usage", lambda path: SimpleNamespace(free=10**12))
    return tmp_path


class Registrar:
    """GitHub as the door sees it: registrations, deletions and runners."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, object]] = []
        self.runners: dict[int, dict] = {}
        self.busy: set[int] = set()

    def request(self, method: str, path: str, body: object = None) -> dict:
        self.sent.append((method, path, body))
        if path.endswith("/generate-jitconfig"):
            runner_id = 100 + len(self.runners)
            self.runners[runner_id] = {"status": "offline", "busy": False}
            return {"runner": {"id": runner_id}, "encoded_jit_config": "SklU"}
        runner_id = int(path.rsplit("/", 1)[1])
        if runner_id not in self.runners:
            raise urllib.error.HTTPError(path, 404, "gone", None, io.BytesIO())  # type: ignore[arg-type]
        if method == "DELETE":
            if runner_id in self.busy:
                raise urllib.error.HTTPError(path, 422, "busy", None, io.BytesIO())  # type: ignore[arg-type]
            del self.runners[runner_id]
            return {}
        return self.runners[runner_id]

    def forget(self, drop) -> None:
        pass


def peer(**kw) -> sup.Peer:
    return sup.Peer("token", served=frozenset({REPO}), **kw)


def registered(gh: Registrar, slot: int = 0, door: sup.Peer | None = None) -> list[str]:
    return sup.peer_register(door or peer(), gh, str(slot), REPO).split()


def test_registers_a_build_runner_only_for_a_served_repo_and_a_slot_the_beelink_allows(
    folders,
) -> None:
    gh = Registrar()
    assert sup.peer_register(peer(), gh, "3", REPO) == "no"
    assert sup.peer_register(peer(), gh, "0", "octo/other") == "no"
    assert sup.peer_register(peer(), gh, "x", REPO) == "no"
    assert gh.sent == []
    ok, name, label, jit = registered(gh)
    assert (ok, label, jit) == ("ok", "-", "SklU")
    assert sup.PEER_RUNNER.fullmatch(name)
    assert gh.sent[0][2]["labels"] == list(sup.RUNNER_LABELS)  # type: ignore[index]
    # Its job's items may still come until its outbox is uploaded.
    assert json.loads((sup.OUTBOX_DIR / f"{name}.json").read_text())["repo"] == REPO
    assert sup.read_peer(name)["runner_id"] == 100  # type: ignore[index]


def test_a_runner_gets_the_cache_label_of_the_oldest_waiting_build_else_the_last(folders) -> None:
    servo = ("octo/app", "fc-cache-octo--servo")
    blog = ("octo/app", "fc-cache-octo--blog")
    assert registered(Registrar(), 0)[2] == "-"
    assert registered(Registrar(), 1, peer(last=servo))[2] == servo[1]
    assert registered(Registrar(), 2, peer(wants=(blog,), last=servo))[2] == blog[1]


def test_a_slot_is_registered_again_only_after_a_pause_and_once_its_runner_is_idle(
    folders, monkeypatch
) -> None:
    gh = Registrar()
    now = [1793990000.0]
    monkeypatch.setattr(sup.time, "time", lambda: now[0])
    first = registered(gh)[1]
    now[0] += 10
    assert sup.peer_register(peer(), gh, "0", REPO) == "no"  # too soon
    now[0] += sup.PEER_REGISTER_EVERY
    gh.busy.add(100)
    assert sup.peer_register(peer(), gh, "0", REPO) == "no"  # it has a job
    gh.busy.clear()
    assert registered(gh)[0] == "ok"
    assert sup.read_peer(first)["gone"]  # type: ignore[index]


def test_ended_outboxes_skip_a_pc_record_until_its_outbox_arrives(folders) -> None:
    state = sup.State()
    record = sup.OUTBOX_DIR / f"{PC_REVIEWER}.json"
    record.write_text(json.dumps({"repo": REPO, "light": False}))
    assert sup.ended_outboxes(state, 100) == ([], [])
    record.with_suffix(".tar").write_bytes(b"homelab-ci-handoff v1 run=70 attempt=1\n")
    assert sup.ended_outboxes(state, 100)[0] == [record.with_suffix(".tar")]


def upload(runner: str, size: int, body: bytes) -> tuple[str, bytes]:
    near, far = socket.socketpair()
    far.sendall(body)
    far.shutdown(socket.SHUT_WR)
    reply = sup.peer_outbox(peer(), runner, size, near.makefile("rb"), near.makefile("wb"), near)
    far.settimeout(0.2)
    try:
        said = far.recv(64)
    except TimeoutError:
        said = b""
    near.close()
    return reply, said


def test_an_outbox_is_uploaded_whole_or_not_at_all(folders) -> None:
    gh = Registrar()
    name = registered(gh)[1]
    tar = sup.OUTBOX_DIR / f"{name}.tar"
    assert upload(name, 10, b"short") == ("no", b"send\n")
    assert not tar.exists() and not list(sup.OUTBOX_DIR.glob(".*.part"))
    assert upload(name, sup.OUTBOX_BYTES + 1, b"")[0] == "no"
    assert upload(name, 5, b"12345") == ("saved", b"send\n")
    assert tar.read_bytes() == b"12345"
    # Sent again after a lost reply: already here.
    assert upload(name, 5, b"12345") == ("saved", b"")


def test_an_empty_outbox_ends_the_wait_for_its_items(folders) -> None:
    name = registered(Registrar())[1]
    assert upload(name, 0, b"") == ("saved", b"")
    assert not (sup.OUTBOX_DIR / f"{name}.json").exists()


def test_the_door_refuses_a_runner_it_did_not_register(folders) -> None:
    near, _ = socket.socketpair()
    for words in (
        ["vm", "pc-1-1793990000"],
        ["vm", "fc-1-1793990000"],
        ["outbox", "../x", "5"],
        ["release", "pc-1-1"],
        ["cache", "octo/app", "-"],  # no cache disk to give
        ["sudo", "sh"],
    ):
        assert sup.peer_answer(peer(), Registrar(), words, io.BytesIO(), io.BytesIO(), near) == "no"


def test_the_sweep_forgets_gone_runners_and_deletes_dead_or_old_idle_ones(folders) -> None:
    gh = Registrar()
    names = [registered(gh, slot)[1] for slot in range(3)]
    gh.runners[101]["status"] = "online"
    gh.runners[102]["status"] = "online"
    gh.busy.add(102)
    del gh.runners[100]  # its job ended and it removed itself
    sup.sweep_peer(gh, peer(), sup.read_peer(names[0])["issued"] + 60)  # type: ignore[index]
    assert sup.read_peer(names[0])["gone"]  # type: ignore[index]
    assert 101 in gh.runners and 102 in gh.runners
    sup.sweep_peer(gh, peer(), sup.read_peer(names[1])["issued"] + sup.STANDBY_RECYCLE_SECONDS + 1)  # type: ignore[index]
    assert 101 not in gh.runners  # idle too long: a fresh one replaces it
    assert 102 in gh.runners  # busy
    later = sup.read_peer(names[0])["gone"] + sup.OUTBOX_WAIT_SECONDS + 1  # type: ignore[index]
    sup.sweep_peer(gh, peer(), later)
    assert sup.read_peer(names[0]) is None
    assert not (sup.OUTBOX_DIR / f"{names[0]}.json").exists()


def test_a_pc_vms_lines_are_checked_and_its_ask_answered_down_the_link(folders) -> None:
    """The whole path: the PC's console reader forwards, the Beelink reads the lines as a
    local VM's and answers the ask, and the PC writes the answer on its VM's inbox."""
    file_item(sup.HANDOFF_DIR, 70, "build", "builder", ["review"], [])
    sup.PEER_DIR.joinpath(f"{PC_REVIEWER}.json").write_text(
        json.dumps({"repo": REPO, "runner_id": 7, "issued": 0, "asks": 0})
    )
    runs = Runs()
    runs.jobs[70][1]["runner_name"] = PC_REVIEWER
    console = (
        b"booting\n"
        b"KAIZEN-LIVE 1 12 builder 1793990000 reading files\n"
        b"KAIZEN-LIVE 1 12 builder 1793990000 bad\x07line\n"
        b"HOMELAB-HANDOFF 1 abcdef12 70 1 70 build builder 0\n"
    )
    forwarded: list[bytes] = []
    sup.read_console(io.BytesIO(console), None, forward=forwarded.append)
    assert forwarded == console.splitlines(keepends=True)[1:]

    near, far = socket.socketpair()
    beelink = threading.Thread(
        target=sup.peer_vm,
        args=(peer(), runs, PC_REVIEWER, near.makefile("rb"), near.makefile("wb"), near),
    )
    beelink.start()
    far.sendall(b"".join(forwarded))
    pc_inbox = folders / "pc.inbox"
    pc_inbox.write_bytes(sup.INBOX_EMPTY.ljust(1 << 16, b"\0"))
    stream = sup.PeerStream.__new__(sup.PeerStream)
    stream.inbox, stream.asks = pc_inbox, {b"HOMELAB-HANDOFF": forwarded[-1]}
    answering = threading.Thread(target=stream.answers, args=(far.makefile("rb"),), daemon=True)
    answering.start()
    for _ in range(100):
        if pc_inbox.read_bytes().startswith(b"homelab-ci-inbox v1 abcdef12"):
            break
        time.sleep(0.1)
    far.shutdown(socket.SHUT_WR)
    beelink.join(timeout=10)
    answering.join(timeout=10)
    inbox = pc_inbox.read_bytes()
    assert inbox.startswith(b"homelab-ci-inbox v1 abcdef12 ok 7\n")
    assert inbox[512:519] == b"payload"
    assert stream.asks == {}
    live = json.loads((sup.PEER_DIR / f"{PC_REVIEWER}.live").read_text())
    assert live["text"] == "reading files"
    assert sup.read_peer(PC_REVIEWER)["asks"] == 1  # type: ignore[index]


def test_a_pc_vms_capacity_ask_is_forwarded_and_never_replaces_a_waiting_handoff() -> None:
    handoff = b"HOMELAB-HANDOFF 1 abcdef12 70 1 70 build builder 600\n"
    capacity = b"HOMELAB-CAPACITY 1 0123456789abcdef\n"
    forwarded: list[bytes] = []
    sup.read_console(io.BytesIO(handoff + capacity), None, forward=forwarded.append)
    assert forwarded == [handoff, capacity]

    stream = sup.PeerStream.__new__(sup.PeerStream)
    stream.lines, stream.asks, stream.sent = queue.Queue(), {}, set()
    for line in forwarded:
        stream.put(line)
    link = io.BytesIO()
    stream.send_asks(link)
    assert link.getvalue() == handoff + capacity
    # The capacity answer comes first; the handoff stays to be sent again after a reconnect.
    stream.answered(b"homelab-ci-inbox v1 0123456789abcdef ok 9\n".ljust(512, b"\0"))
    stream.sent.clear()
    again = io.BytesIO()
    stream.send_asks(again)
    assert again.getvalue() == handoff


def test_only_fresh_pc_live_lines_show(folders) -> None:
    (sup.PEER_DIR / "pc-1-1793990000.live").write_text(json.dumps({"heard": 1000, "text": "a"}))
    (sup.PEER_DIR / "pc-2-1793990000.live").write_text(json.dumps({"heard": 900, "text": "b"}))
    assert [line["text"] for line in sup.peer_live(1000 + sup.PEER_LIVE_SECONDS - 50)] == ["a"]


def test_an_outbox_is_sent_up_to_its_first_hole_and_only_with_a_header(folders) -> None:
    tar = folders / "out.tar"
    tar.write_bytes(b"homelab-ci-outbox\n")
    assert sup.outbox_end(tar) == 0
    tar.write_bytes(b"homelab-ci-cache v1 run=70 attempt=1\n".ljust(8192, b"x"))
    assert sup.outbox_end(tar) == 0  # the Beelink keeps no cache from a PC VM
    with tar.open("wb") as f:
        f.write(b"homelab-ci-handoff v1 run=70 attempt=1\n".ljust(8192, b"x"))
        f.truncate(sup.OUTBOX_BYTES)
    assert 8192 <= sup.outbox_end(tar) < 1 << 20


def test_the_pc_slot_count_allows_zero(folders, monkeypatch) -> None:
    path = folders / "pc-slots"
    monkeypatch.setattr(sup, "PC_SLOTS_FILE", path)
    monkeypatch.setattr(sup, "SLOTS_FILE", folders / "slots")
    (folders / "slots").write_text("8\n")
    assert sup.pc_slot_count() == 0
    path.write_text("0\n")
    assert sup.pc_slot_count() == 0
    path.write_text("6\n")
    assert sup.pc_slot_count() == 6
    path.write_text("12\n")
    assert sup.pc_slot_count() == 8


def test_a_pc_vm_with_no_job_behind_it_gets_one_github_lookup(folders) -> None:
    runs = Runs()
    inbox = folders / "x.inbox"
    known: dict = {}
    for own in (80, 81, 82):
        inbox.write_bytes(sup.INBOX_EMPTY.ljust(1 << 16, b"\0"))
        line = f"HOMELAB-HANDOFF 1 abcdef12 {own} 1 70 build builder 0\n".encode()
        ask = sup.HANDOFF_ASK.fullmatch(line)
        assert sup.answer_ask(runs, REPO, PC_REVIEWER, inbox, ask, known) == "denied"
    assert len([p for p in runs.asked if "/jobs" in p]) == 1


def test_a_pc_vms_outbox_never_becomes_a_cache_disk(folders) -> None:
    tar = sup.OUTBOX_DIR / f"{PC_REVIEWER}.tar"
    tar.write_bytes(b"homelab-ci-cache v1 run=70 attempt=1\n".ljust(1024, b"\0"))
    tar.with_suffix(".json").write_text(json.dumps({"repo": REPO, "light": False}))
    assert sup.take_outbox(Runs(), tar, {REPO: "main"}, 0) is False
    assert not sup.cache_image(REPO).exists()


def test_idle_pc_runners_cover_young_jobs_and_a_new_label_replaces_an_idle_one(folders) -> None:
    gh = Registrar()
    door = peer(last=("octo/app", "fc-cache-octo--servo"))
    name = registered(gh, 0, door)[1]
    gh.runners[100]["status"] = "online"
    issued = sup.read_peer(name)["issued"]  # type: ignore[index]
    sup.sweep_peer(gh, door, issued + 30)
    assert sup.peer_ready(issued + 60) == {("octo/app", "fc-cache-octo--servo"): 1}
    assert sup.peer_ready(issued + 30 + sup.PEER_SEEN_SECONDS) == {}
    # A build for another product waits: the idle runner goes, a fresh one fits it.
    door.wants = (("octo/app", "fc-cache-octo--blog"),)
    sup.sweep_peer(gh, door, issued + sup.PEER_RELABEL_SECONDS + 1)
    assert sup.read_peer(name)["gone"]  # type: ignore[index]
    assert sup.peer_ready(issued + sup.PEER_RELABEL_SECONDS + 2) == {}


def test_a_registration_reply_comes_back_whole(monkeypatch) -> None:
    jit = "J" * 6000  # a real one-job registration is several KB of base64
    script = f"import sys; sys.stdin.readline(); print('ok pc-0-1793990000 - {jit}')"
    monkeypatch.setattr(sup, "peer_ssh", lambda: [sys.executable, "-c", script])
    assert sup.peer_call("register 0 octo/app").split()[3] == jit


def test_the_status_file_shows_the_pcs_slots_and_busy_runners(folders, monkeypatch) -> None:
    gh = Registrar()
    door = peer()
    now = 1793990000.0
    monkeypatch.setattr(sup.time, "time", lambda: now)
    assert sup.peer_answer(door, gh, ["slots", "9"], io.BytesIO(), io.BytesIO(), None) == "ok"  # type: ignore[arg-type]
    names = [registered(gh, slot, door)[1] for slot in range(2)]
    gh.runners[100]["status"] = gh.runners[101]["status"] = "online"
    gh.busy.add(100)
    gh.runners[100]["busy"] = True
    sup.sweep_peer(gh, door, now)
    assert sup.peer_status(door, now) == {
        "up": True,
        "slots": 3,
        "in_use": 1,
    }  # 3: the Beelink's limit
    assert sup.peer_status(door, now + sup.PEER_HEARD_SECONDS) == {
        "up": False,
        "slots": 0,
        "in_use": 0,
    }
    assert names


def test_the_door_says_which_commit_and_image_the_beelink_runs(folders, monkeypatch) -> None:
    commit, images = folders / "commit", folders / "images"
    images.mkdir()
    monkeypatch.setattr(sup, "COMMIT_FILE", commit)
    monkeypatch.setattr(sup, "IMAGES_DIR", images)
    near, _ = socket.socketpair()
    ask = lambda *w: sup.peer_answer(peer(), Registrar(), list(w), io.BytesIO(), io.BytesIO(), near)  # noqa: E731
    assert ask("version") == "- -"
    commit.write_text("a" * 40 + "\n")
    (images / "sha256").write_text("b" * 64 + "\n")
    assert ask("version") == f"{'a' * 40} {'b' * 64}"
    assert ask("image", "../x") == "no"
    assert ask("image", "b" * 64) == "same"


def test_the_pc_takes_the_beelinks_image_only_when_it_hashes_right(folders, monkeypatch) -> None:
    beelink, pc = folders / "beelink", folders / "pc"
    for d in (beelink, pc):
        d.mkdir()
    (beelink / "rootfs.ext4").write_bytes(b"root" * 1000)
    (beelink / "vmlinuz").write_bytes(b"kernel")
    sums = subprocess.run(
        "sha256sum rootfs.ext4 vmlinuz | sha256sum | cut -c1-64",
        shell=True,
        cwd=beelink,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (beelink / "sha256").write_text(sums + "\n")
    door = peer()
    sent = io.BytesIO()
    monkeypatch.setattr(sup, "IMAGES_DIR", beelink)
    near, _ = socket.socketpair()
    assert sup.peer_image(door, "-", sent, near) == ""
    assert door.image_sent and not door.caching.locked()
    stream = folders / "stream"
    stream.write_bytes(sent.getvalue())
    # The PC side, its door replaced by a script that answers with what the Beelink sent.
    monkeypatch.setattr(sup, "IMAGES_DIR", pc)
    monkeypatch.setattr(sup, "peer_ssh", lambda: ["sh", "-c", f"read line; cat {stream}"])
    assert sup.fetch_image("c" * 64) == 1  # not the hash `version` named: nothing switches
    assert not (pc / "rootfs.ext4").exists()
    assert sup.fetch_image(sums) == 0
    assert (pc / "rootfs.ext4").read_bytes() == b"root" * 1000
    assert (pc / "sha256").read_text().strip() == sums
    assert not (pc / ".incoming").exists()


def test_image_copies_are_limited_per_hour(folders, monkeypatch) -> None:
    images = folders / "images"
    images.mkdir()
    (images / "sha256").write_text("b" * 64 + "\n")
    monkeypatch.setattr(sup, "IMAGES_DIR", images)
    door = peer(image_sent=[time.time()] * sup.IMAGE_SENDS_PER_HOUR)
    near, _ = socket.socketpair()
    assert sup.peer_image(door, "-", io.BytesIO(), near) == "busy"
