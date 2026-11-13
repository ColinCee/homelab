"""Throwaway: a test past TEST_LIMIT must still fail CI, waiting or computing."""

import time

import conftest


def test_a_long_wait() -> None:
    time.sleep(conftest.TEST_LIMIT + 1)


def test_a_long_computation() -> None:
    end = time.process_time() + conftest.TEST_LIMIT + 1
    while time.process_time() < end:
        pass
