"""The line run-vm.sh prints when a CI VM ends: its memory peak and what it wrote."""

import os
import re
import subprocess
from pathlib import Path

CI_RUNNER = Path(__file__).parent.parent / "ci-runner"


def end_line(tmp_path: Path, stat: str | None, outbox_bytes: int = 0) -> str:
    """Run run-vm.sh's cleanup with slot 3's root snapshot on dm-7 and a fake stat file."""
    sys_block = tmp_path / "sys"
    (sys_block / "dm-7").mkdir(parents=True)
    (sys_block / "dm-9").mkdir()
    if stat is not None:
        (sys_block / "dm-7" / "stat").write_text(stat)
    # Another slot's snapshot, which must not count.
    (sys_block / "dm-9" / "stat").write_text("0 0 0 0 0 0 999999 0 0 0 0\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dmsetup = bin_dir / "dmsetup"
    dmsetup.write_text("#!/bin/sh\necho 'ci-slot3-root dm-7'\necho 'ci-slot13-root dm-9'\n")
    dmsetup.chmod(0o755)
    outbox = tmp_path / "outbox.img"
    outbox.write_bytes(b"x" * outbox_bytes)
    peak = tmp_path / "peak"
    peak.write_text(str(300 * 1048576))
    cleanup = re.search(
        r"^cleanup\(\) \{\n.*?^\}\n", (CI_RUNNER / "run-vm.sh").read_text(), re.S | re.M
    )
    assert cleanup
    script = f"""
source {CI_RUNNER / "vm-disks.sh"}
detach_disks() {{ :; }}
{cleanup.group(0)}
slot=3 status="exit 0" MEM_MIB=2048 peak_file={peak} cgroup=/nonexistent base={tmp_path}
id=x CACHE_OUT={outbox} INBOX={tmp_path / "missing"}
sleep 60 & sampler=$!
cleanup
"""
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SYS_BLOCK": str(sys_block)},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stderr.strip()


def test_the_end_line_gives_memory_peak_and_bytes_written(tmp_path: Path) -> None:
    # 409600 sectors written to the snapshot is 200 MiB; the outbox adds its 3 MiB of blocks.
    stat = "  100  0  800  5  2000  0  409600  90  0  50  95\n"
    line = end_line(tmp_path, stat, outbox_bytes=3 * 1048576)

    assert (
        line == "slot 3: VM ended (exit 0), its memory peaked at 300 MiB of 2048, it wrote 203 MiB"
    )


def test_an_unreadable_counter_gives_a_question_mark(tmp_path: Path) -> None:
    line = end_line(tmp_path, stat=None)

    assert line.endswith("its memory peaked at 300 MiB of 2048, it wrote ? MiB")
