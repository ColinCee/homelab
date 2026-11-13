"""Validate every Compose service and profile with each stack's tracked settings, on Linux and
Windows alike, with the Docker CLI alone: `config` needs no daemon, and secret files need not exist,
as Compose checks them only when it starts a container.

    python3 checks/validate_compose.py [compose.yaml ...]   (none means every stack's)

Standard library only: ci.py runs it with the runner's system Python, before uv is installed.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

Run = Callable[[list[str]], int]


def command(compose: Path) -> list[str]:
    settings = compose.parent / "settings.env"
    env_file = ["--env-file", str(settings)] if settings.is_file() else []
    return [
        "docker",
        "compose",
        *env_file,
        "-f",
        str(compose),
        "--profile",
        "*",
        "config",
        "--quiet",
    ]


def main(argv: Sequence[str], run: Run = lambda c: subprocess.run(c).returncode) -> int:
    files = [Path(f) for f in argv] or sorted(Path("stacks").glob("*/compose.yaml"))
    for compose in files:
        print(f"Validating {compose.as_posix()}...", flush=True)
        if code := run(command(compose)):
            return code
    print("All compose files valid")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
