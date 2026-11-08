import subprocess
from pathlib import Path

import pytest

from scripts.adr_frozen import breaches

RECORD = "docs/adr/0001-store.md"
EVIDENCE = "docs/adr/0001-store/results.md"


def _record(status: str, decision: str = "Use the store.") -> str:
    return f"# Store\n\n## Status\n\n{status}\n\n## Decision\n\n{decision}\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str | None]) -> str:
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git(tmp_path, "init", "-q")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _change(repo: Path, start: dict[str, str | None], change: dict[str, str | None]) -> list[str]:
    base = _commit(repo, start)
    return breaches(base, _commit(repo, change))


def test_an_accepted_record_may_change_its_status(repo: Path) -> None:
    start = {RECORD: _record("Accepted")}
    assert _change(repo, start, {RECORD: _record("Superseded by 0002")}) == []


def test_an_accepted_record_may_not_change_its_decision(repo: Path) -> None:
    start = {RECORD: _record("Accepted")}
    assert _change(repo, start, {RECORD: _record("Accepted", "Use another.")}) != []


def test_an_accepted_record_may_not_be_removed(repo: Path) -> None:
    start = {RECORD: _record("Accepted"), "README.md": "x"}
    assert _change(repo, start, {RECORD: None}) != []


def test_an_accepted_records_evidence_is_frozen(repo: Path) -> None:
    start = {RECORD: _record("Accepted"), EVIDENCE: "614 MiB"}
    assert _change(repo, start, {EVIDENCE: "600 MiB"}) != []


def test_a_proposed_record_changes_freely_and_may_be_accepted(repo: Path) -> None:
    start = {RECORD: _record("Proposed"), EVIDENCE: "614 MiB"}
    change = {RECORD: _record("Accepted", "Use another."), EVIDENCE: "600 MiB"}
    assert _change(repo, start, change) == []


def test_a_new_record_passes(repo: Path) -> None:
    start = {RECORD: _record("Accepted")}
    assert _change(repo, start, {"docs/adr/0002-other.md": _record("Accepted")}) == []
