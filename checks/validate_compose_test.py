"""Compose validation (checks/validate_compose.py)."""

from pathlib import Path

import pytest

from checks import validate_compose
from conftest import ROOT, stacks


def test_no_file_named_checks_every_stack_with_its_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(ROOT)
    ran: list[list[str]] = []
    assert validate_compose.main([], lambda c: ran.append(c) or 0) == 0

    assert [Path(c[c.index("-f") + 1]) for c in ran] == [
        Path("stacks") / s.name / "compose.yaml" for s in stacks()
    ]
    for c in ran:
        compose = Path(c[c.index("-f") + 1])
        settings = compose.parent / "settings.env"
        assert ("--env-file" in c) == settings.is_file(), compose


def test_the_first_invalid_file_fails_the_check(capsys: pytest.CaptureFixture[str]) -> None:
    ran: list[list[str]] = []
    assert (
        validate_compose.main(["a/compose.yaml", "b/compose.yaml"], lambda c: ran.append(c) or 3)
        == 3
    )
    assert len(ran) == 1 and "All compose files valid" not in capsys.readouterr().out
