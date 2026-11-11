#!/usr/bin/env python3
"""Write each GitHub secret the deploy passes to its own file, secrets/<its name in lower case>.

A stack's compose.yaml mounts the ones it needs under /run/secrets (Compose `secrets:`, `file:`):
a secret never goes in a container's environment, which anything allowed to inspect containers can
read. The deploy workflow's render step is the one list of secret names; this reads its `env:`.
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/deploy.yaml")
FROM_SECRET = re.compile(r"^\$\{\{\s*secrets\.([A-Z0-9_]+)\s*\}\}$")


class RenderError(ValueError):
    """Raised when a secret cannot be rendered."""


def secret_names(repo_root: Path) -> tuple[str, ...]:
    """The variables the workflow's step running this script takes from GitHub secrets."""
    workflow = yaml.safe_load((repo_root / WORKFLOW).read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "render-secrets" in step.get("run", ""):
                env = step.get("env") or {}
                return tuple(sorted(n for n, v in env.items() if FROM_SECRET.match(str(v))))
    raise RenderError(f"No step in {WORKFLOW} runs render-secrets")


def render(repo_root: Path, environment: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Check every secret is set, then write each file that changed."""
    environment = os.environ if environment is None else environment
    names = secret_names(repo_root)
    missing = [name for name in names if not environment.get(name)]
    if missing:
        raise RenderError("Missing required environment variable(s): " + ", ".join(missing))
    folder = repo_root / "secrets"
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    written = tuple(folder / name.lower() for name in names)
    for path, name in zip(written, names, strict=True):
        # No trailing newline: not every reader trims one.
        _write(path, environment[name].rstrip("\r\n"))
    return written


def _write(path: Path, content: str) -> None:
    """Containers read it as different users, so the file is open to all and its folder to the
    owner alone. It is rewritten only when it changes, so its time marks a new value, which
    recreates the containers reading it (deploy.py, stack_config_hash)."""
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(content)
        temporary.chmod(0o444)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Wrote {path}")


def main() -> int:
    try:
        render(Path(__file__).resolve().parents[1])
    except (RenderError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
