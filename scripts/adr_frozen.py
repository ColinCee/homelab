"""Fail a change that rewrites an accepted decision record.

Usage: adr_frozen.py BASE HEAD. A record (docs/adr/NNNN-title.md) accepted at
BASE must still exist at HEAD with only its status line changed, and its
evidence folder (docs/adr/NNNN-title/) must be unchanged. A changed decision is
a new record that supersedes the old one. Exits 1 and names each breach. CI
runs the base commit's copy, so a pull request cannot change the rule that
judges it.
"""

from __future__ import annotations

import re
import subprocess
import sys

RECORD = re.compile(r"docs/adr/\d{4}-[a-z0-9-]+\.md")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def _files(commit: str, path: str) -> dict[str, str]:
    """Each file under path at commit, by its blob id."""
    out = _git("ls-tree", "-r", commit, "--", path).splitlines()
    return {line.split("\t", 1)[1]: line.split()[2] for line in out}


def _split(text: str) -> tuple[str, str]:
    """The status line, and the record without it."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## Status":
            for j in range(i + 1, len(lines)):
                if lines[j].strip():
                    return lines[j].strip(), "\n".join(lines[:j] + lines[j + 1 :])
    return "", text


def breaches(base: str, head: str) -> list[str]:
    before, after = _files(base, "docs/adr"), _files(head, "docs/adr")
    found = []
    for path in sorted(p for p in before if RECORD.fullmatch(p)):
        status, body = _split(_git("show", f"{base}:{path}"))
        if not status.lower().startswith("accepted"):
            continue
        if path not in after:
            found.append(f"{path}: an accepted record was removed or renamed")
        elif _split(_git("show", f"{head}:{path}"))[1] != body:
            found.append(f"{path}: an accepted record changed beyond its status line")
        folder = path.removesuffix(".md") + "/"
        evidence = {p: b for p, b in before.items() if p.startswith(folder)}
        if evidence != {p: b for p, b in after.items() if p.startswith(folder)}:
            found.append(f"{folder}: an accepted record's evidence changed")
    return found


if __name__ == "__main__":
    problems = breaches(sys.argv[1], sys.argv[2])
    for problem in problems:
        print(f"{problem}; write a new record that supersedes it instead", file=sys.stderr)
    sys.exit(1 if problems else 0)
