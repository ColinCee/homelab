#!/usr/bin/env python3
"""ci-checkout: put the job's commit in the workspace, for homelab's own jobs in a CI VM.

actions/checkout spent about a second in Node and in writing then removing a token the job never
keeps; this runs three git commands. The repo is public, so the fetch needs no token. A pull
request also gets its base (the merge commit's first parent), which scripts/ci.py diffs against.
Standard library only: it runs before the job has any code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path


def commands(env: Mapping[str, str]) -> list[list[str]]:
    sha = env["GITHUB_SHA"]
    url = f"{env['GITHUB_SERVER_URL']}/{env['GITHUB_REPOSITORY']}"
    depth = "2" if env.get("GITHUB_EVENT_NAME") == "pull_request" else "1"
    return [
        ["git", "-c", "init.defaultBranch=main", "init", "-q"],
        # As actions/checkout leaves it, for a later step that fetches more.
        ["git", "remote", "add", "origin", url],
        [
            "git",
            "-c",
            "protocol.version=2",
            "fetch",
            "-q",
            "--no-tags",
            f"--depth={depth}",
            "origin",
            f"+{sha}:refs/remotes/origin/checkout",
        ],
        ["git", "-c", "advice.detachedHead=false", "checkout", "-q", "--detach", sha],
    ]


def main(env: Mapping[str, str] = os.environ) -> int:
    workspace = Path(env["GITHUB_WORKSPACE"])
    workspace.mkdir(parents=True, exist_ok=True)
    for entry in workspace.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for command in commands(env):
        if code := subprocess.run(command, cwd=workspace).returncode:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
