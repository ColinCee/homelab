"""CI's time limits (conftest.py): a slow test or run is named, and only past its limit."""

import conftest


def test_only_a_test_or_run_past_its_limit_is_named() -> None:
    calls = {"fast": conftest.TEST_LIMIT - 0.1, "slow": conftest.TEST_LIMIT + 0.1}
    over = conftest.over_limits(conftest.RUN_LIMIT - 1, calls)
    assert len(over) == 1 and over[0].startswith("slow ")
    assert len(conftest.over_limits(conftest.RUN_LIMIT + 1, {})) == 1
