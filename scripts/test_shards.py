"""Experiment: the test files split into one balanced share per core, one pytest process each, so
each process collects only its own files (pytest-xdist makes every worker collect them all)."""

import os
import subprocess
import sys
import time
from pathlib import Path

# Seconds per file, measured alone on a CI VM (12 Nov); others about 0.35 s.
WEIGHT = {
    "test_alert_rules.py": 1.6, "test_update.py": 2.2, "test_dashboards.py": 0.8, "test_deploy.py": 1.4,
    "test_fc_cache_action.py": 1.0, "test_ci_supervisor.py": 0.8, "test_pin_only.py": 0.76,
    "test_install_host.py": 0.75, "test_gateway.py": 0.7, "test_versions_hash.py": 0.66,
    "test_backup.py": 0.63, "test_probes.py": 0.6, "test_alloy.py": 0.58, "test_ci.py": 0.58,
    "test_no_secret_in_env.py": 0.54, "test_run_vm.py": 0.6,
}


def main() -> int:
    files = sorted(Path("tests").glob("test_*.py"), key=lambda f: -WEIGHT.get(f.name, 0.35))
    n = os.cpu_count() or 1
    shares: list[list[str]] = [[] for _ in range(n)]
    load = [0.0] * n
    for f in files:
        i = load.index(min(load))
        shares[i].append(str(f))
        load[i] += WEIGHT.get(f.name, 0.35)
    start = time.monotonic()
    procs = [
        subprocess.Popen([sys.executable, "-m", "pytest", "-q", "-p", "no:xdist", *share],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for share in shares if share
    ]
    code = 0
    for p in procs:
        out, _ = p.communicate()
        print(out.strip().splitlines()[-1] if out.strip() else "", f"({time.monotonic() - start:.2f} s)")
        if p.returncode:
            print(out)
            code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
