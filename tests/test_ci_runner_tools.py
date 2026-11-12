"""The image tools check (ci-runner/tests/test_tools.py)."""

import importlib.util
import json
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tools_test_script", Path(__file__).parents[1] / "ci-runner/tests/test_tools.py"
)
assert SPEC is not None and SPEC.loader is not None
tools = importlib.util.module_from_spec(SPEC)
sys.modules["tools_test_script"] = tools
SPEC.loader.exec_module(tools)


def run(console: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, dict[str, str]]:
    sent: dict[str, str] = {}

    def boot(command: list[str], log: Path, env: Mapping[str, str]) -> None:
        assert command[0] == "/stub/run-vm.sh" and command[1:2] == ["0"]
        sent.update(json.loads(Path(command[2]).read_text()))
        log.write_text(console)

    host = tools.Host(boot=boot, env={"RUN_VM": "/stub/run-vm.sh"}, is_root=lambda: True)
    code = tools.main(host)
    assert isinstance(code, int)
    return code, capsys.readouterr().out, sent


def test_every_counted_tool_passing_passes(capsys: pytest.CaptureFixture[str]) -> None:
    console = "boot\r\nPASS node --version: v24\r\nPASS uv --version: uv 0.9\r\nTOTAL 2\r\n"
    code, out, sent = run(console, capsys)
    assert code == 0
    passed = "PASS node --version: v24\nPASS uv --version: uv 0.9\n"
    assert out == passed + "tools test passed (2 tools)\n"
    assert sent["task"] == "probe" and sent["probe"] == tools.PROBE


@pytest.mark.parametrize(
    ("console", "line"),
    [
        ("PASS a: 1\nFAIL b: missing\nTOTAL 2\n", "TOOLS TEST FAILED (1 of 2 passed)"),
        ("PASS a: 1\n", "TOOLS TEST FAILED (1 of ? passed)"),
    ],
)
def test_a_failed_or_uncounted_tool_fails(
    console: str, line: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(console, capsys)
    assert code == 1 and line in out


def test_a_busy_slot_fails_and_says_why(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_vm = tmp_path / "run-vm.sh"  # what run-vm.sh does when a job holds slot 0
    run_vm.write_text('#!/usr/bin/env bash\necho "slot $1 is busy" >&2\nexit 75\n')
    run_vm.chmod(0o755)
    host = tools.Host(env={"RUN_VM": str(run_vm)}, is_root=lambda: True)

    assert tools.main(host) == 1
    out = capsys.readouterr().out
    assert "TOOLS TEST FAILED (0 of ? passed)" in out
    assert "slot 0 is busy" in out


def test_a_missing_run_vm_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    host = tools.Host(env={"RUN_VM": str(tmp_path / "absent")}, is_root=lambda: True)
    assert tools.main(host) == 1
    assert "TOOLS TEST FAILED (0 of ? passed)" in capsys.readouterr().out
