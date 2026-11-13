"""Tests run on Colin's Windows PC as well as in CI's Linux (AGENTS.md): every module imports on
both, and a test needing Linux says so through conftest.py's linux_only, which fails in CI rather
than skip."""

import ast
import re
import sys

import pytest

from conftest import is_test, linux_only, tracked

# Standard library modules Windows has none of.
LINUX_MODULES = {"fcntl", "grp", "pwd", "termios", "tty", "pty", "resource", "syslog", "posix"}
PLATFORM_CHECK = re.compile(r"\bsys\.platform\b|\bos\.name\b|\bplatform\.system\(")


def top_level_imports(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module.split(".")[0])
    return found


def test_no_module_imports_what_only_linux_has_at_its_top() -> None:
    """Imported in the function that uses it, so its tests import on Windows too."""
    found = {
        path.name: sorted(top_level_imports(path.read_text(encoding="utf-8")) & LINUX_MODULES)
        for path in tracked(".py")
    }
    assert {name: mods for name, mods in found.items() if mods} == {}


def test_no_test_checks_the_platform_but_through_linux_only() -> None:
    """A skipif of its own would skip in CI as well, unseen."""
    found = [
        path.name
        for path in tracked(".py")
        if is_test(path)
        and path.name != "conftest.py"
        and PLATFORM_CHECK.search(path.read_text(encoding="utf-8"))
    ]
    assert found == []


def test_off_linux_it_skips_but_fails_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.skip.Exception, match="needs Linux: flock"):
        linux_only("flock")
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception, match="flock"):
        linux_only("flock")
    monkeypatch.setattr(sys, "platform", "linux")
    linux_only("flock")
