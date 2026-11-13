"""Say whether a diff only moves image pins, so CI can skip the slow checks.

Usage: pin_only.py BASE HEAD. Exits 0 when every changed file is an existing
stacks/*/compose.yaml and every changed line is an `image:` line whose new
value is pinned by digest; exits 1 otherwise. CI runs the base commit's copy,
so a pull request cannot change the rule that judges it.
"""

from __future__ import annotations

import re
import subprocess
import sys

COMPOSE = re.compile(r"stacks/[^/]+/compose\.yaml")
IMAGE = re.compile(r"\s*image:\s*\S+\s*")
PINNED = re.compile(r"\s*image:\s*[\w./:-]+:[\w.-]+@sha256:[0-9a-f]{64}\s*")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def is_pin_only(base: str, head: str) -> bool:
    changes = _git("diff", "--no-renames", "--name-status", base, head).splitlines()
    if not changes:
        return False
    for change in changes:
        status, path = change.split("\t", 1)
        if status != "M" or not COMPOSE.fullmatch(path):
            return False
        diff = _git("diff", "--unified=0", base, head, "--", path)
        hunks = diff.partition("\n@@")[2].splitlines()
        removed = [line[1:] for line in hunks if line[:1] == "-"]
        added = [line[1:] for line in hunks if line[:1] == "+"]
        if not added or len(added) != len(removed):
            return False
        if not all(IMAGE.fullmatch(line) for line in removed):
            return False
        if not all(PINNED.fullmatch(line) for line in added):
            return False
    return True


if __name__ == "__main__":
    sys.exit(0 if is_pin_only(sys.argv[1], sys.argv[2]) else 1)
