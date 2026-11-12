"""The load test itself (ci-runner/tests/test_load.py): its PASS or FAIL decision, and the
last line, which the supervisor keeps as the result (ci_supervisor.py, drain_and_test)."""

import importlib.util
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "load_test_script", Path(__file__).parents[1] / "ci-runner/tests/test_load.py"
)
assert SPEC is not None and SPEC.loader is not None
load = importlib.util.module_from_spec(SPEC)
sys.modules["load_test_script"] = load
SPEC.loader.exec_module(load)

BOOTED = "=== probe start\nBURN_MEM 1900 MiB\nBURN_DONE\n"
Answer = Callable[[str, bool, float], str]


class VM:
    def __init__(self, code: int) -> None:
        self.code = code

    def wait(self) -> int:
        return self.code


def ok(url: str, loaded: bool, now: float) -> str:
    return "0.012000 200"


def make_host(
    root: Path, answer: Answer = ok, logs: Mapping[int, tuple[int, str]] | None = None
) -> tuple[load.Host, list[list[str]]]:
    (root / "proc").mkdir(exist_ok=True)
    (root / "proc/meminfo").write_text(
        "MemTotal: 32000000 kB\nMemAvailable: 8192000 kB\nDirty: 2048 kB\nWriteback: 1024 kB\n"
    )
    (root / "proc/loadavg").write_text("9.10 4.20 1.30 3/900 1234\n")
    clock = [0.0]
    loaded = [False]
    started: list[list[str]] = []

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        match command:
            case ["ip", "-4", "-br", "addr", "show", "tailscale0"]:
                out = "tailscale0 UNKNOWN 100.64.0.5/32 fd7a::5/128\n"
            case ["curl", *_, url]:
                out = answer(url, loaded[0], clock[0]) + "\n"
            case _:
                raise AssertionError(f"unexpected command {command}")
        return subprocess.CompletedProcess(command, 0, out)

    def start(command: list[str], log: Path, env: Mapping[str, str]) -> VM:
        assert env["VCPUS"] == "2" and env["MEM_MIB"] == "2048"
        loaded[0] = True
        started.append(command)
        code, text = (logs or {}).get(int(command[1]), (0, BOOTED))
        log.write_text(text)
        return VM(code)

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    host = load.Host(
        run=run,
        start=start,
        root=root,
        sleep=sleep,
        clock=lambda: clock[0],
        env={"SLOTS": "3", "RUN_VM": "/lib/run-vm.sh"},
        is_root=lambda: True,
    )
    return host, started


def last_line(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().out.splitlines()[-1]


def test_every_vm_booted_and_every_answer_quick_passes(tmp_path, capsys) -> None:
    host, started = make_host(tmp_path)
    assert load.main(["20"], host) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[-1] == (
        "PASS 3 slots: every VM booted and finished, no request failed, worst response within 1s"
    )
    assert [c[:2] for c in started] == [["/lib/run-vm.sh", str(n)] for n in range(3)]
    assert started[0][3] == "140"  # the burn's seconds and two minutes to boot and stop
    assert "slot 2: held 1900 MiB, burned and exited" in out
    assert "loaded Grafana: median 0.012s, worst 0.012s at 9s, 0 failed" in out
    assert "host lowest MemAvailable: 8000 MiB" in out
    assert "host most data waiting for the disk: 3 MiB" in out


def test_a_slow_answer_under_load_fails_with_the_service_and_its_time(tmp_path, capsys) -> None:
    def slow_grafana(url: str, loaded: bool, now: float) -> str:
        return "1.400000 200" if loaded and url.endswith(":5080/healthz") else "0.010000 200"

    host, _ = make_host(tmp_path, slow_grafana)
    assert load.main(["20"], host) == 1
    assert last_line(capsys) == "FAIL 3 slots: loaded Grafana: worst 1.400000s over 1s"


def test_max_response_raises_the_bound(tmp_path, capsys) -> None:
    def slowish(url: str, loaded: bool, now: float) -> str:
        return "1.400000 200"

    host, _ = make_host(tmp_path, slowish)
    host.env = {**host.env, "MAX_RESPONSE": "2"}
    assert load.main(["20"], host) == 0
    assert last_line(capsys).endswith("worst response within 2s")


def test_failed_requests_fail_and_show_when_they_came(tmp_path, capsys) -> None:
    def refused_at_boot(url: str, loaded: bool, now: float) -> str:
        # The quiet phase takes the first 15 seconds.
        return "0.000000 000" if loaded and url.endswith(":8123/") and now < 17 else "0.01 200"

    host, _ = make_host(tmp_path, refused_at_boot)
    assert load.main(["20"], host) == 1
    out = capsys.readouterr().out
    # Equal times rank by the line as text, so second 9 sorts after second 14.
    assert "loaded Home Assistant: median 0.010s, worst 0.010s at 9s, 2 failed" in out
    assert "(code@second: 000@0s 000@1s)" in out
    assert out.splitlines()[-1] == "FAIL 3 slots: loaded Home Assistant: 2 requests failed"


def test_a_vm_that_did_not_boot_or_burn_fails_with_its_log(tmp_path, capsys) -> None:
    logs = {1: (1, "slot 1 is busy\n"), 2: (0, "=== probe start\nBURN_MEM 1900 MiB\n")}
    host, _ = make_host(tmp_path, logs=logs)
    assert load.main(["20"], host) == 1
    out = capsys.readouterr().out
    assert "slot 1: did not boot; its log:\nslot 1 is busy\n" in out
    assert out.splitlines()[-1] == (
        "FAIL 3 slots: slot 1: VM did not boot (exit 1); slot 2: VM did not finish its burn"
    )


def test_a_quiet_failure_counts_too(tmp_path, capsys) -> None:
    def down_before(url: str, loaded: bool, now: float) -> str:
        return "0.01 200" if loaded else "0.000000 000"

    host, _ = make_host(tmp_path, down_before)
    assert load.main(["20"], host) == 1
    assert last_line(capsys).startswith("FAIL 3 slots: quiet Home Assistant: 15 requests failed")


@pytest.mark.parametrize("seconds", ["15", "x", "-20"])
def test_seconds_must_be_a_number_over_15(tmp_path, seconds) -> None:
    host, started = make_host(tmp_path)
    assert load.main([seconds], host) == 2
    assert started == []


def test_without_the_tailnet_address_it_gives_no_verdict(tmp_path, capsys) -> None:
    # The supervisor keeps no PASS or FAIL line as not run, rather than lowering the slots.
    host, started = make_host(tmp_path)

    def no_tailscale(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "")

    host.run = no_tailscale
    assert load.main(["20"], host) == 1
    assert started == []
    assert not [
        line for line in capsys.readouterr().out.splitlines() if line.startswith(("PASS ", "FAIL "))
    ]


def test_it_needs_root(tmp_path) -> None:
    host, started = make_host(tmp_path)
    host.is_root = lambda: False
    assert load.main([], host) == 1 and started == []
