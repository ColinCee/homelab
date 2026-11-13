"""CI's time limits (tests/conftest.py): a slow test or run is named, and only past its limit."""

from pathlib import Path

import conftest
import pytest
from conftest import Mark


def test_only_a_test_or_run_past_its_limit_is_named() -> None:
    calls = {"fast": conftest.TEST_LIMIT - 0.1, "slow": conftest.TEST_LIMIT + 0.1}
    over = conftest.over_limits(conftest.RUN_LIMIT - 1, calls)
    assert len(over) == 1 and over[0].startswith("slow ")
    assert len(conftest.over_limits(conftest.RUN_LIMIT + 1, {})) == 1


def test_only_cpu_time_the_host_took_comes_off() -> None:
    # 13 Nov, a 4-CPU VM's shard beside another VM: 21.9 s, 13.7 s of it on a CPU (with children),
    # while the host took 38.6 s of the VM's CPUs as they ran 35.4 s. It took 15 s alone.
    start = Mark(wall=0, cpu=0, stolen=22.47, busy=19.35)
    assert 13 < conftest.own_seconds(start, Mark(21.9, 13.7, 61.03, 54.74)) < 15
    # A test that only waits loses nothing, however much the host took from the other shards.
    assert conftest.own_seconds(start, Mark(8.0, 0.0, 40.0, 30.0)) == 8.0
    # Nor does anything when the host took nothing.
    assert conftest.own_seconds(start, Mark(8.0, 6.0, 22.47, 40.0)) == 8.0


def test_a_mark_reads_steal_and_busy_seconds(tmp_path: Path) -> None:
    stat = tmp_path / "stat"
    stat.write_text("cpu  1355 2 543 4652 205 3 37 2247 0 0\ncpu0 1 2 3 4 5 6 7 8 0 0\n")
    taken = conftest.mark(stat)
    assert (taken.stolen, taken.busy) == pytest.approx((22.47, 19.4))
    assert conftest.mark(tmp_path / "missing")[2:] == (0.0, 0.0)
