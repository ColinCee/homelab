"""The host tests' one way to run a command (ci-runner/host-tests/host_commands.py)."""

import ast
import sys
from pathlib import Path

from host_commands import run_command, start_process

from conftest import ROOT

CI_RUNNER = ROOT / "ci-runner"
# Each keeps its own copy: the installer and what it installs must run with nothing from the
# checkout (ci-runner/install_host_test.py), and setup_net.py is installed apart, under net/.
OWN_COPY = {"install_host.py", "update.py", "vm_disks.py", "setup_net.py"}
MISSING = "/nonexistent/program"


def test_the_host_tests_run_commands_only_through_host_commands() -> None:
    for path in sorted(CI_RUNNER.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        if path.name in OWN_COPY:
            assert "run_command" in defined, f"{path.name} has no copy now: drop it from OWN_COPY"
            continue
        if path.name == "host_commands.py":
            continue
        assert not defined & {"run_command", "start_process"}, f"{path}: import host_commands"
        if path.parent.name == "tests":
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    called = f"{node.value.id}.{node.attr}"
                    assert called not in {"subprocess.run", "subprocess.Popen"}, (
                        f"{path.name}: {called}; use host_commands"
                    )


def test_a_missing_program_fails_as_a_shell_does(tmp_path: Path) -> None:
    assert run_command([MISSING], quiet=True).returncode == 127
    log = tmp_path / "log"
    assert start_process([MISSING], log).wait() == 127
    assert log.read_text().startswith(f"{MISSING}: ")


def test_a_command_runs_with_its_output_kept(tmp_path: Path) -> None:
    done = run_command([sys.executable, "-c", "print('hi')"])
    assert (done.returncode, done.stdout) == (0, "hi\n")
    log = tmp_path / "log"
    assert start_process([sys.executable, "-c", "print('logged')"], log).wait() == 0
    assert log.read_text() == "logged\n"
