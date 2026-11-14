"""Every secret file a stack reads has a row in docs/recovery.md, "Secrets", and every row a file:
the table is how a rebuild knows what to copy out of Bitwarden."""

import re
from pathlib import Path

from conftest import STACKS, load_yaml

RECOVERY = Path(__file__).parent.parent / "docs" / "recovery.md"
ROW = re.compile(r"^\| `([^`]+)` \|", re.MULTILINE)


def listed(doc: str) -> set[str]:
    section = doc.split("\n## Secrets\n", 1)[1].split("\n## ", 1)[0]
    return set(ROW.findall(section))


def used() -> set[str]:
    names = set()
    for path in [*STACKS.rglob("*.yaml"), *STACKS.rglob("*.yml")]:
        spec = load_yaml(path)
        if not isinstance(spec, dict):
            continue
        for secret in (spec.get("secrets") or {}).values():
            file = str((secret or {}).get("file", ""))
            if file.startswith("../../secrets/"):
                names.add(file.removeprefix("../../secrets/"))
    return names


def test_listed_reads_only_the_secrets_table() -> None:
    doc = "## Other\n| `x` | a |\n## Secrets\n| `a` | b |\n| `b` | c |\n## Next\n| `y` | d |\n"
    assert listed(doc) == {"a", "b"}


def test_every_secret_file_is_listed_and_every_row_used() -> None:
    assert used() == listed(RECOVERY.read_text(encoding="utf-8"))
