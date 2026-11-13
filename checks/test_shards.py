"""Run the tests as one pytest per core, each over its own share of the test files.

pytest-xdist had every worker import and collect every test file, about 4 s of a CI VM's 9 s
test step; here each process collects only its share, which cut the checks by 1.3 s (12 Nov,
CI VMs). Shares are balanced by each file's measured seconds, so a heavy file is not left last.

    uv run python -m checks.test_shards [pytest args...]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

# Seconds per file alone on a CI VM (12 Nov); an unlisted file is about LIGHT. Out of date numbers
# only unbalance the shares a little.
HEAVY = {
    "update_test.py": 2.2,
    "alert_rules_test.py": 1.6,
    "deploy_test.py": 1.4,
    "fc_cache_test.py": 1.0,
    "dashboards_test.py": 0.8,
    "ci_supervisor_test.py": 0.8,
    "pin_only_test.py": 0.76,
    "install_host_test.py": 0.75,
    "gateway_test.py": 0.7,
    "versions_hash_test.py": 0.66,
}
LIGHT = 0.4
# What GitHub keeps of an annotation, as conftest.py's write_slowest caps it.
TIMES_CHARS = 3500
NO_TESTS = 5


def shares(files: Sequence[Path], count: int) -> list[list[Path]]:
    """The files in `count` shares of about equal seconds, heaviest placed first."""
    out: list[list[Path]] = [[] for _ in range(max(1, count))]
    load = [0.0] * len(out)
    for f in sorted(files, key=lambda f: (-HEAVY.get(f.name, LIGHT), f.name)):
        i = load.index(min(load))
        out[i].append(f)
        load[i] += HEAVY.get(f.name, LIGHT)
    return [share for share in out if share]


def merge_times(parts: Sequence[str]) -> str:
    """The shards' slowest-test lists as one, slowest first, under the annotation's size."""
    rows = sorted((row for part in parts for row in json.loads(part or "[]")), key=lambda r: -r[2])
    kept: list[list[object]] = []
    for row in rows:
        if len(json.dumps([*kept, row], separators=(",", ":"))) > TIMES_CHARS:
            break
        kept.append(row)
    return json.dumps(kept, separators=(",", ":"))


def verdict(codes: Sequence[int]) -> int:
    """The run's exit code: a shard a filter (-k) left without tests (pytest's 5) passes, unless
    every shard was."""
    failed = [code for code in codes if code not in (0, NO_TESTS)]
    if failed:
        return failed[0] if failed[0] > 0 else 128 - failed[0]  # killed: 128 + the signal
    return NO_TESTS if codes and all(code == NO_TESTS for code in codes) else 0


def files_to_run() -> list[Path]:
    """Every test file in git: each sits beside what it tests, as `<name>_test.py`."""
    listed = subprocess.run(
        ["git", "ls-files", "*_test.py"], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    return sorted(Path(p) for p in listed)


def main(args: Sequence[str], env: Mapping[str, str] = os.environ) -> int:
    planned = shares(files_to_run(), os.process_cpu_count() or 1)
    times = env.get("TEST_TIMES")
    procs = []
    for n, share in enumerate(planned):
        own = {**env, "TEST_TIMES": f"{times}.{n}"} if times else dict(env)
        command = [sys.executable, "-m", "pytest", "-q", *args, *map(str, share)]
        procs.append(
            subprocess.Popen(
                command, env=own, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
        )
    codes = []
    for proc in procs:
        out, _ = proc.communicate()
        print(out, end="", flush=True)
        codes.append(int(proc.returncode))
    if times:
        parts = [Path(f"{times}.{n}") for n in range(len(planned))]
        texts = [p.read_text(encoding="utf-8") for p in parts if p.exists()]
        Path(times).write_text(merge_times(texts), encoding="utf-8")
        for p in parts:
            p.unlink(missing_ok=True)
    return verdict(codes)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
