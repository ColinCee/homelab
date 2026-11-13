"""Write each systemd unit held as failed, the system's and Colin's, to failed-units.prom for "A
systemd unit failed" (prometheus/rules.yml). Run each minute as Colin by
observability-failed-units.timer, from the checkout on the host's /usr/bin/python3: the security
facts' writer runs as its own user, which cannot reach his user manager.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# The shared module, from this checkout (stacks/telemetry).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telemetry"))
import telemetry

OUT = Path("/var/lib/homelab-metrics/failed-units.prom")
METRIC = "homelab_systemd_unit_failed"
MANAGERS = {"system": [], "user": ["--user"]}


def failed(manager: str) -> list[str]:
    """Fails when systemd does not answer, so the old file stays and its stopped rule fires."""
    command = ["systemctl", *MANAGERS[manager], "list-units", "--state=failed", "--output=json"]
    listed = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
    return sorted(unit["unit"] for unit in json.loads(listed.stdout))


def render(units: dict[str, list[str]]) -> str:
    lines = [
        f"# HELP {METRIC} A unit systemd holds as failed (1), by its manager.",
        f"# TYPE {METRIC} gauge",
    ]
    lines += [
        f'{METRIC}{{manager="{manager}",unit="{telemetry.label(name)}"}} 1'
        for manager, names in units.items()
        for name in names
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    telemetry.replace_file(OUT, render({manager: failed(manager) for manager in MANAGERS}))


if __name__ == "__main__":
    telemetry.start()
    main()
