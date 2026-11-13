"""Copy Home Assistant's history database consistently, for the nightly backup
(systemd/home-assistant-history.service), as one trace. Run from the checkout on the host's
/usr/bin/python3.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The shared module, from this checkout (stacks/telemetry).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telemetry"))
import telemetry

# Run by Home Assistant's own Python, which has no shared module. SQLite's backup API gives a
# consistent copy of the live database, replaced whole so restic never reads half of one.
COPY = (
    "import os, sqlite3; s = sqlite3.connect('/config/home-assistant_v2.db');"
    " d = sqlite3.connect('/config/history-snapshot.db.tmp'); s.backup(d);"
    " d.execute('pragma journal_mode=delete'); d.close();"
    " os.replace('/config/history-snapshot.db.tmp', '/config/history-snapshot.db')"
)


def main() -> int:
    with telemetry.run("home-assistant-history", "copy history") as trace:
        copied = telemetry.command(
            "copy", ["docker", "exec", "homeassistant", "python3", "-c", COPY]
        )
        if not copied:
            trace.fail("copy failed")
        return 0 if copied else 1


if __name__ == "__main__":
    telemetry.start()
    raise SystemExit(main())
