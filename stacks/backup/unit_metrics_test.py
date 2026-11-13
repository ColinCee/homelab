"""The backup units' metrics (stacks/backup/unit_metrics.py), which rules.yml's alerts read."""

import os
from pathlib import Path

from stacks.backup.unit_metrics import pc_received, upkeep


def test_the_newest_received_file_is_the_metric(tmp_path: Path) -> None:
    folder, out = tmp_path / "pc-backups", tmp_path / "metrics"
    (folder / "laptop").mkdir(parents=True)
    out.mkdir()
    pc_received(folder, out)
    assert not (out / "pc-backup.prom").exists()  # nothing received yet: no number
    for name, when in (("old.tar", 1_000), ("laptop/new.tar", 2_000)):
        (folder / name).write_text("x")
        os.utime(folder / name, (when, when))
    pc_received(folder, out)
    text = (out / "pc-backup.prom").read_text()
    assert text == "homelab_pc_backup_newest_timestamp_seconds 2000\n"


def test_upkeep_records_its_result_by_name(tmp_path: Path) -> None:
    upkeep("success", 1_700.5, tmp_path)
    upkeep("exit-code", 1_800.0, tmp_path)
    success = (tmp_path / "upkeep-success.prom").read_text()
    failure = (tmp_path / "upkeep-failure.prom").read_text()
    assert success == "homelab_upkeep_last_success_timestamp_seconds 1700\n"
    assert failure == "homelab_upkeep_last_failure_timestamp_seconds 1800\n"
