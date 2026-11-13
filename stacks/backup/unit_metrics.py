"""The backup units' metrics for Prometheus (prometheus/rules.yml), run by their units from the
checkout on the host's /usr/bin/python3:

    unit_metrics.py pc-received   the newest file in ~/pc-backups (backup-pc-receive.service)
    unit_metrics.py upkeep        when upkeep last succeeded or failed (backup-upkeep.service)
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

# The shared module, from this checkout (stacks/telemetry).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telemetry"))
import telemetry

METRICS = Path("/var/lib/homelab-metrics")
log = logging.getLogger("backup-unit-metrics")


def pc_received(folder: Path, out: Path) -> None:
    times = [p.stat().st_mtime for p in folder.rglob("*") if p.is_file()]
    if not times:
        log.info("no PC backups received yet")
        return
    telemetry.replace_file(
        out / "pc-backup.prom", f"homelab_pc_backup_newest_timestamp_seconds {int(max(times))}\n"
    )


def upkeep(service_result: str, now: float, out: Path) -> None:
    # SERVICE_RESULT, which systemd gives ExecStopPost.
    result = "success" if service_result == "success" else "failure"
    telemetry.replace_file(
        out / f"upkeep-{result}.prom", f"homelab_upkeep_last_{result}_timestamp_seconds {int(now)}\n"
    )


def main(argv: list[str]) -> int:
    match argv:
        case ["pc-received"]:
            pc_received(Path.home() / "pc-backups", METRICS)
        case ["upkeep"]:
            upkeep(os.environ.get("SERVICE_RESULT", ""), time.time(), METRICS)
        case _:
            log.error("usage: unit_metrics.py pc-received | upkeep")
            return 2
    return 0


if __name__ == "__main__":
    telemetry.start()
    raise SystemExit(main(sys.argv[1:]))
