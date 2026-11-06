import pytest

from scripts import control_room_timing as timing

T = timing.parse_time


def _timing() -> timing.Timing:
    return timing.compute(
        merged=T("2026-11-05T10:00:00Z"),
        image_done=T("2026-11-05T10:03:20Z"),
        pr_opened=T("2026-11-05T10:03:40Z"),
        pr_merged=T("2026-11-05T10:05:10Z"),
        deploy_started=T("2026-11-05T10:05:30Z"),
        live=T("2026-11-05T10:07:00Z"),
    )


def test_splits_merge_to_live_into_image_pull_request_and_deploy() -> None:
    assert _timing() == timing.Timing(total=420, image=200, pull_request=90, deploy=90)


def test_notice_line_is_one_annotation_with_every_part() -> None:
    assert timing.notice(_timing()) == (
        "::notice title=Control room live::420s after merge "
        "(image 200s, pull request 90s, deploy 90s)"
    )


def test_summary_table_lists_each_part() -> None:
    text = timing.summary(_timing(), "abcdef1234567890")

    assert "Control room live 420s after merge" in text
    assert "`abcdef1`" in text
    assert "| Merge to image built | 200 |" in text
    assert "| Homelab pull request open to merged | 90 |" in text
    assert "| Deploy job start to live | 90 |" in text


def test_image_end_comes_from_the_main_image_run() -> None:
    runs = [
        {"name": "check", "event": "push", "updated_at": "2026-11-05T10:01:00Z"},
        {"name": "image", "event": "pull_request", "updated_at": "2026-11-05T09:50:00Z"},
        {"name": "image", "event": "push", "updated_at": "2026-11-05T10:03:20Z"},
    ]

    assert timing.image_run_end(runs) == T("2026-11-05T10:03:20Z")
    with pytest.raises(LookupError):
        timing.image_run_end(runs[:2])


def test_pull_request_times_come_from_the_merged_one() -> None:
    pulls = [
        {"created_at": "2026-11-05T09:00:00Z", "merged_at": None},
        {"created_at": "2026-11-05T10:03:40Z", "merged_at": "2026-11-05T10:05:10Z"},
    ]

    assert timing.merged_pull(pulls) == (T("2026-11-05T10:03:40Z"), T("2026-11-05T10:05:10Z"))


OLD, NEW = "a" * 40, "b" * 40


def test_live_is_the_first_answer_naming_the_commit_from_the_ready_time() -> None:
    answers = [
        (T("2026-11-05T10:06:00Z"), NEW),  # before the stack came up: not counted
        (T("2026-11-05T10:06:30Z"), OLD),  # the old container, still answering
        (T("2026-11-05T10:06:40Z"), NEW),
        (T("2026-11-05T10:06:50Z"), NEW),
    ]

    assert timing.first_live(answers, NEW, T("2026-11-05T10:06:20Z")) == T("2026-11-05T10:06:40Z")
    assert timing.first_live(answers[:2], NEW, T("2026-11-05T10:06:20Z")) is None


def test_watcher_log_is_read_back_and_a_cut_line_is_skipped(tmp_path) -> None:
    log = tmp_path / "state.jsonl"
    log.write_text(
        f'{{"at": "2026-11-05T10:06:30+00:00", "commit": "{OLD}"}}\n'
        f'{{"at": "2026-11-05T10:06:40+00:00", "commit": "{NEW}"}}\n'
        '{"at": "2026-11-05T10:0',
        encoding="utf-8",
    )

    assert timing.read_answers(log) == [
        (T("2026-11-05T10:06:30Z"), OLD),
        (T("2026-11-05T10:06:40Z"), NEW),
    ]
    assert timing.read_answers(tmp_path / "missing.jsonl") == []


def test_polling_skips_a_stale_commit_and_waits_for_the_deployed_one() -> None:
    replies = iter([None, OLD, OLD, NEW])
    asked: list[str | None] = []

    def ask(_: str) -> str | None:
        asked.append(reply := next(replies))
        return reply

    timing.wait_for_commit("http://state", NEW, interval=0, ask=ask)

    assert asked == [None, OLD, OLD, NEW]
    with pytest.raises(TimeoutError):
        timing.wait_for_commit("http://state", NEW, timeout=0, interval=0, ask=lambda _: OLD)
