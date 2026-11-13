"""Throwaway: a test past TEST_LIMIT must still fail CI, waiting or computing."""

import time

import conftest


def test_a_long_wait() -> None:
    time.sleep(conftest.TEST_LIMIT + 1)


def test_a_long_computation() -> None:
    # CPU time counts steal, which comes off, so burn enough that half of it is still too long.
    end = time.process_time() + conftest.TEST_LIMIT * 3
    while time.process_time() < end:
        pass
