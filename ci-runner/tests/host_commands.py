"""How the host tests in this folder run commands, one way for all of them, so a fix reaches each
(tests/test_ci_runner_tools.py fails a copy). They find it beside them: in this folder, or in
/usr/local/lib/homelab-ci, where install_host.py installs it with test_load.py."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path


def run_command(
    command: list[str],
    *,
    env: Mapping[str, str] | None = None,
    check: bool = False,
    quiet: bool = False,
    show: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Stdout is captured unless SHOW sends it to the terminal; QUIET drops stderr. A missing
    program fails as a shell's 127, so the probe behind it fails rather than the whole test."""
    try:
        return subprocess.run(
            command,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            stdout=None if show else subprocess.PIPE,
            stderr=subprocess.DEVNULL if quiet else None,
            env=env,
            check=check,
        )
    except OSError as e:
        if check:
            raise
        if not quiet:
            print(f"{command[0]}: {e}", file=sys.stderr)
        return subprocess.CompletedProcess(command, 127, "")


class NotStarted:
    """A command whose program is missing: its log says why, and it ended as a shell's 127."""

    def wait(self, timeout: float | None = None) -> int:
        return 127


def start_process(
    command: list[str], log: Path, env: Mapping[str, str] | None = None
) -> subprocess.Popen[bytes] | NotStarted:
    """COMMAND running with its stdout and stderr in LOG."""
    with log.open("w") as out:
        try:
            return subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env
            )
        except OSError as e:
            out.write(f"{command[0]}: {e}\n")
            return NotStarted()
