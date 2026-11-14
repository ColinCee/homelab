"""The monthly upkeep (systemd/backup-upkeep.service), one trace with a span per step: prune Docker
images and build cache unused for a week, so a deploy running alongside loses nothing, then
test-restore a file from the latest backup, whose own spans join this trace. Run from the checkout
on the host's /usr/bin/python3; its unit records the result for the alert (unit_metrics.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

# The shared module, from this checkout (stacks/telemetry).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telemetry"))
import telemetry

STEPS = (
    ("prune images", ["docker", "image", "prune", "-af", "--filter", "until=168h"]),
    ("prune build cache", ["docker", "builder", "prune", "-af", "--filter", "until=168h"]),
    (
        "restore test",
        [
            *("docker", "exec", "-e", "TRACEPARENT", "backup"),
            *("python3", "-I", "/backup.py", "restore-test"),
        ],
    ),
)


def main() -> int:
    with telemetry.run("backup-upkeep", "upkeep") as trace:
        # Every step runs: a failed prune does not skip the restore test.
        failed = [name for name, argv in STEPS if not telemetry.command(name, argv)]
        if failed:
            trace.fail(f"{', '.join(failed)} failed")
        return 1 if failed else 0


if __name__ == "__main__":
    telemetry.start()
    raise SystemExit(main())
