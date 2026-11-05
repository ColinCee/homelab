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
