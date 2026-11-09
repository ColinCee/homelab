"""The heartbeat watch (scripts/heartbeat_watch.py) posts once when the heartbeat goes 2 hours
without passing, reminds every 4 hours, and clears once, however late its own runs are."""

from scripts.heartbeat_watch import decide

H = 3600.0


def test_quiet_while_the_heartbeat_passes() -> None:
    assert decide([0, 15 * 60], 1 * H, 0) is None


def test_posts_once_when_it_goes_two_hours_without_passing() -> None:
    assert decide([0], 2.5 * H, 1.5 * H) == "firing"
    assert decide([0], 3.5 * H, 2.5 * H) is None


def test_reminds_every_four_hours() -> None:
    assert decide([0], 5.9 * H, 4.9 * H) is None
    assert decide([0], 6.2 * H, 5.2 * H) == "firing"
    # A late or skipped watch still reminds once.
    assert decide([0], 11 * H, 6.2 * H) == "firing"


def test_clears_once_when_it_passes_again() -> None:
    assert decide([0, 4 * H], 4.2 * H, 3.2 * H) == "cleared"
    assert decide([0, 4 * H], 5.2 * H, 4.2 * H) is None


def test_a_heartbeat_that_never_passed_fires_and_reminds() -> None:
    assert decide([], 1 * H, None) == "firing"
    assert decide([], 2 * H, 1 * H) is None
    assert decide([], 4.1 * H, 3.1 * H) == "firing"


def test_the_first_watch_counts_the_time_before_as_fine() -> None:
    assert decide([0], 3 * H, None) == "firing"
    assert decide([0], 1 * H, None) is None
