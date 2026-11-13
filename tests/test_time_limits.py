"""CI's time limits (tests/conftest.py): a slow test or run is named, and only past its limit."""

import conftest


def test_only_a_test_or_run_past_its_limit_is_named() -> None:
    calls = {"fast": conftest.TEST_LIMIT - 0.1, "slow": conftest.TEST_LIMIT + 0.1}
    over = conftest.over_limits(conftest.RUN_LIMIT - 1, calls)
    assert len(over) == 1 and over[0].startswith("slow ")
    assert len(conftest.over_limits(conftest.RUN_LIMIT + 1, {})) == 1


def stat(steal: int) -> str:
    """/proc/stat as a CI VM writes it (13 Nov), with `steal` hundredths over all its CPUs."""
    return f"cpu  1351 0 543 4652 205 0 37 {steal} 0 0\ncpu0 338 0 135 1163 51 0 9 561 0 0\n"


def test_only_cpu_time_the_host_took_comes_off_the_clock() -> None:
    # 13 Nov, beside another VM: a 4-CPU VM's shard took 21.9 s while the host took 38.6 CPU s.
    took = 21.9 - (conftest.stolen_seconds(stat(6103), 4) - conftest.stolen_seconds(stat(2246), 4))
    assert 12 < took < 13
    # Outside a VM steal stays 0, so a test that waits keeps all its seconds; a line that is not
    # the CPUs' total takes nothing off.
    assert conftest.stolen_seconds(stat(0), 4) == 0
    assert conftest.stolen_seconds("intr 1 2 3 4 5 6 7 8 9", 4) == 0
