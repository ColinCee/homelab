"""Every secret file a stack reads has a row in docs/secrets.md, and every row a file:
the table is where to find every secret, and how a rebuild knows what to copy out of Bitwarden."""

import re
from pathlib import Path

from conftest import STACKS, load_yaml

SECRETS_DOC = Path(__file__).parent.parent / "docs" / "secrets.md"
ROW = re.compile(r"^\| `([^`]+)` \|", re.MULTILINE)


def listed(doc: str) -> set[str]:
    return set(ROW.findall(doc))


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


def test_listed_reads_table_rows_only() -> None:
    doc = "Text `x` here.\n\n| `secrets/` file | Gives |\n| --- | --- |\n| `a` | b |\n| `b` | c |\n"
    assert listed(doc) == {"a", "b"}


def test_every_secret_file_is_listed_and_every_row_used() -> None:
    assert used() == listed(SECRETS_DOC.read_text(encoding="utf-8"))
