"""The host's own load test (ci-runner/host-tests/test_load.py): when it is due, the drain that
gives it the host, and its result, which a failure turns into one slot fewer."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import vms

log = logging.getLogger("ci-supervisor").info


# How many VMs run at once, read from /etc/homelab-ci/slots at start-up. install_host.py
# writes it and makes that many slot users and taps, so the three always match. A job
# alone gets 4 GiB and each one started beside it 2 GiB (see vm_size), so N slots use
# up to 2 + 2N GiB on paper. The default 5 (12 GiB on paper) fits the 16 GiB host because
# jobs mostly wait on the network and touch far less than their size, and the memory
# check below holds a VM back rather than eat into the services' reserve.
SLOTS_FILE = Path("/etc/homelab-ci/slots")
DEFAULT_SLOTS = 5


# The host's own load test (ci-runner/host-tests/test_load.py), run by LOAD_TEST_UNIT once for each
# slot count and version of the test (docs/architecture.md, "CI runner"). Its
# result goes in the status file and in each VM at /etc/homelab-ci/load-test.json.
LOAD_TEST_FILE = Path("/var/lib/homelab-ci/load-test.json")
# Hashed itself, so a change to the test makes a new version.
LOAD_TEST_SCRIPT = Path("/usr/local/lib/homelab-ci/test_load.py")
LOAD_TEST_UNIT = "homelab-ci-load-test.service"
# While this file exists no VM starts and the standbys stop, so the test has the host to itself.
# The unit removes it however it ends (ExecStopPost), and /run is empty after a reboot.
DRAIN_FILE = Path("/run/homelab-ci/load-test-drain")
# A job VM's own limit, and a little: every running job ends within it.
DRAIN_LIMIT_SECONDS = vms.VM_TIMEOUT_SECONDS + 300
DRAIN_LOOK_SECONDS = 10
# A failed test lowers the slot count by one, never below this: fewer is Colin's call.
MIN_TESTED_SLOTS = 3
# A test that did not run (jobs outlasted the drain, or the script broke) is tried again after this.
LOAD_TEST_RETRY_SECONDS = 6 * 3600
# update.py's lock: no install or image build adds load while the test runs.
UPDATE_LOCK = Path("/run/homelab-ci-update.lock")


def slot_count() -> int:
    """The host's slot count: DEFAULT_SLOTS if the file is missing or not a number, at least 1."""
    try:
        text = SLOTS_FILE.read_text().strip()
    except OSError:
        return DEFAULT_SLOTS
    return max(1, int(text)) if text.isdecimal() else DEFAULT_SLOTS


def read_load_test() -> dict[str, object] | None:
    """The last load test's record, or None if there is none or it is not a JSON object."""
    try:
        record = json.loads(LOAD_TEST_FILE.read_text())
    except OSError, ValueError:
        return None
    return {str(k): v for k, v in record.items()} if isinstance(record, dict) else None


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_test_due(record: Mapping[str, object] | None, slots: int, test: str, now: float) -> bool:
    """Whether to run the load test: never run at this slot count and test version, or it did
    not run then (the record has no result) and LOAD_TEST_RETRY_SECONDS have passed. A failed
    test is not run again: it lowered the count, and the lower count is due."""
    if record is None or record.get("slots") != slots or record.get("test") != test:
        return True
    at = record.get("at")
    return not record.get("ran") and (
        not isinstance(at, int | float) or now - at >= LOAD_TEST_RETRY_SECONDS
    )


def start_load_test_if_due() -> int:
    """`start-load-test-if-due`, from update.py: start LOAD_TEST_UNIT when the test is due."""
    try:
        test = file_hash(LOAD_TEST_SCRIPT)
    except OSError as e:
        log(f"no load test to run: {type(e).__name__}: {e}")
        return 0
    if load_test_due(read_load_test(), slot_count(), test, time.time()):
        log(f"starting {LOAD_TEST_UNIT}: no result for {slot_count()} slots and this test")
        # Refused once the unit's start limit is reached: it is started again after that.
        if subprocess.run(
            ["systemctl", "start", "--no-block", LOAD_TEST_UNIT], timeout=30
        ).returncode:
            log(f"systemd did not start {LOAD_TEST_UNIT}; tried again on the next pass")
    return 0


def run_load_test() -> int:
    """`load-test`, LOAD_TEST_UNIT's work: drain, run test_load.py at every slot, keep the
    result, and on a FAIL lower the slot count by one for the next run to test. Whatever
    breaks after the drain starts is kept as "not run", so the test waits
    LOAD_TEST_RETRY_SECONDS instead of draining the host again on every update pass."""
    slots, test = slot_count(), file_hash(LOAD_TEST_SCRIPT)
    commit = read_word(COMMIT_FILE) or ""
    kept = False

    def keep(ran: bool, passed: bool, line: str) -> None:
        nonlocal kept
        record = {"slots": slots, "passed": passed, "ran": ran, "line": line}
        record |= {"at": int(time.time()), "commit": commit, "test": test}
        vms.replace_file(LOAD_TEST_FILE, json.dumps(record))
        kept = True
        log(f"load test at {slots} slots: {line}")

    DRAIN_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    DRAIN_FILE.touch()
    try:
        return drain_and_test(slots, keep)
    except Exception as e:
        if not kept:
            keep(False, False, f"not run: {type(e).__name__}: {e}")
        raise
    finally:
        DRAIN_FILE.unlink(missing_ok=True)


def drain_and_test(slots: int, keep: Callable[[bool, bool, str], None]) -> int:
    """Wait for the running VMs to end, then run test_load.py and keep its result."""
    deadline = time.monotonic() + DRAIN_LIMIT_SECONDS
    log("draining: no new VMs; waiting for the running ones to end")
    # None (systemd did not answer) is not known to be empty: wait on.
    while (running := vms.running_vms()) != set():
        if time.monotonic() >= deadline:
            count = "some" if running is None else str(len(running))
            keep(False, False, f"not run: {count} VMs still running after the drain limit")
            return 1
        time.sleep(DRAIN_LOOK_SECONDS)
    import fcntl  # Linux only, and only here, so the tests import this module on any system.

    line = ""
    with UPDATE_LOCK.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with subprocess.Popen(
            [str(LOAD_TEST_SCRIPT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "SLOTS": str(slots)},
        ) as proc:
            assert proc.stdout is not None
            for out in proc.stdout:
                if out.strip():
                    level = logging.ERROR if out.startswith("FAIL ") else logging.INFO
                    logging.getLogger("ci-supervisor").log(level, out.rstrip("\n"))
                if out.startswith(("PASS ", "FAIL ")):
                    line = out.strip()
        code = proc.returncode
    passed = code == 0 and line.startswith("PASS ")
    if not line:
        keep(False, False, f"not run: test_load.py exited {code} with no PASS or FAIL line")
        return 1
    keep(True, passed, line)
    if not passed and slots > MIN_TESTED_SLOTS:
        SLOTS_FILE.write_text(f"{slots - 1}\n")
        log(f"lowered the slot count to {slots - 1}; the next update tests it")
    return 0 if passed else 1


# The commit install_host.py last installed (ci-runner/update.py writes it).
COMMIT_FILE = Path("/etc/homelab-ci/commit")


def read_word(path: Path) -> str:
    try:
        return path.read_text().split()[0]
    except OSError, IndexError:
        return ""
