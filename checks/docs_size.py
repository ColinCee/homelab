"""Fail when a doc grows past its word count in docs-size.json.

Docs grow one appended line per change until nobody reads them, so growth has to be a choice:
run `mise run docs:size -- --update` and commit the new baseline, where the pull request shows
it. A doc that shrinks passes; updating the baseline then lowers its cap. Decision records
themselves are left out, since each keeps to one page (the recording-learnings skill); their
evidence folders and the repo's skills are capped.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BASELINE = Path("docs-size.json")


def docs() -> list[Path]:
    """The tracked Markdown files the cap covers."""
    out = subprocess.run(
        ["git", "ls-files", "README.md", "AGENTS.md", "docs/*.md", ".github/skills/*/SKILL.md"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(Path(p) for p in out.split() if Path(p).parent != Path("docs/adr"))


def words(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").split())


def grown(sizes: dict[str, int], baseline: dict[str, int]) -> list[str]:
    """Each doc over its cap, or new and not in the baseline."""
    return [
        f"{name}: {size} words, cap {baseline[name]}"
        if name in baseline
        else f"{name}: {size} words, not in {BASELINE}"
        for name, size in sizes.items()
        if size > baseline.get(name, 0)
    ]


def main(argv: list[str]) -> int:
    sizes = {p.as_posix(): words(p) for p in docs()}
    if argv == ["--update"]:
        BASELINE.write_text(json.dumps(sizes, indent=1) + "\n", encoding="utf-8", newline="\n")
        print(f"{BASELINE}: {sum(sizes.values())} words in {len(sizes)} docs")
        return 0
    over = grown(sizes, json.loads(BASELINE.read_text(encoding="utf-8")))
    for line in over:
        print(line)
    if over:
        print("Cut the doc back, or if the growth is needed: mise run docs:size -- --update")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
