"""The image tools check (ci-runner/tests/test-tools.sh)."""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "ci-runner" / "tests" / "test-tools.sh"


def test_a_busy_slot_fails_and_says_why(tmp_path: Path) -> None:
    # The root check is the first guard; drop it so the rest runs unprivileged.
    script = tmp_path / "test-tools.sh"
    root_check = '[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }\n'
    assert root_check in SCRIPT.read_text()
    script.write_text(SCRIPT.read_text().replace(root_check, ""))
    run_vm = tmp_path / "run-vm.sh"  # what run-vm.sh does when a job holds slot 0
    run_vm.write_text('#!/usr/bin/env bash\necho "slot $1 is busy" >&2\nexit 75\n')
    run_vm.chmod(0o755)

    result = subprocess.run(
        ["bash", str(script)],
        env={**os.environ, "RUN_VM": str(run_vm)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "TOOLS TEST FAILED (0 of ? passed)" in result.stdout
    assert "slot 0 is busy" in result.stdout
