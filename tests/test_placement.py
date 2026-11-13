"""Where files live: each top-level folder is one job, and a module, unit or test sits beside what
it serves (AGENTS.md). The TODO sets hold what the layout move still has to fix, and only shrink."""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from functools import cache
from pathlib import PurePosixPath

import pytest
from conftest import ROOT, is_test, tracked

# A new top-level folder is added here with its one job, or not at all.
FOLDERS = {
    ".github": "the workflows, the fc-cache action and the agent skills",
    "docs": "the architecture, recovery and decision records",
    "stacks": "each Compose stack the deploy brings up, with its programs, units and config",
    "ci-runner": "the Firecracker CI runner the host installs",
    "beelink": "the host's own system setup and health jobs",
    "checks": "what a pull request must pass",
    "tailnet": "who may reach what on the tailnet",
}

# Each entry fails once it is fixed, so these only shrink.
TODO_FOLDERS = {"scripts", "tests"}
TODO_UNITS = {
    "ci-runner/systemd/homelab-ci-load-test.service",
    "ci-runner/systemd/homelab-ci-net.service",
    "ci-runner/systemd/homelab-ci-supervisor.service",
    "ci-runner/systemd/homelab-ci-times.service",
}

# Named in docs, but kept on the host, not in git.
HOST_ONLY = {"stacks/home-assistant/config"}

# A unit's program: installed by the host installer, or run from the deploy's checkout.
PROGRAM = re.compile(r"(?:/usr/local/lib/homelab-ci/|%h/code/homelab/)([\w./-]+)")


@cache
def paths() -> tuple[str, ...]:
    return tuple(p.relative_to(ROOT).as_posix() for p in tracked())


def is_test_path(path: str) -> bool:
    return is_test(ROOT / path)


def python_files() -> list[str]:
    return [p for p in paths() if p.endswith(".py") and not p.endswith("__init__.py")]


def by_name() -> dict[str, list[str]]:
    found: dict[str, list[str]] = defaultdict(list)
    for path in python_files():
        found[PurePosixPath(path).stem].append(path)
    return found


def unlisted_folders() -> set[str]:
    return {p.split("/")[0] for p in paths() if "/" in p} - FOLDERS.keys()


def test_each_top_level_folder_has_one_job() -> None:
    assert unlisted_folders() <= TODO_FOLDERS, "add it to FOLDERS with its job, or move it"
    assert not TODO_FOLDERS - unlisted_folders(), "moved: take it off TODO_FOLDERS"


def test_no_program_lies_loose_at_the_top() -> None:
    loose = [p for p in paths() if "/" not in p and p.endswith((".py", ".sh"))]
    assert loose == [], "put it in the folder for its job"


def test_python_names_are_unique() -> None:
    """The host installs its scripts into one folder, where they import each other by name."""
    assert {n: ps for n, ps in by_name().items() if len(ps) > 1} == {}


def importers() -> dict[str, set[str]]:
    """Each module's importers, tests left out: a test is not a user."""
    names = by_name()
    found: dict[str, set[str]] = defaultdict(set)
    for path in python_files():
        if is_test_path(path):
            continue
        for node in ast.walk(ast.parse((ROOT / path).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules = [node.module]
            else:
                continue
            for module in modules:
                for target in names.get(module.rsplit(".", 1)[-1], []):
                    if target != path and not is_test_path(target):
                        found[target].add(path)
    return found


def test_a_module_one_other_uses_sits_beside_it() -> None:
    far = {
        module: user
        for module, users in importers().items()
        if len(users) == 1
        and PurePosixPath(user := next(iter(users))).parent != PurePosixPath(module).parent
    }
    assert far == {}, "move each module into its one user's folder"


def units_away_from_their_program() -> set[str]:
    names = {PurePosixPath(p).name: p for p in paths()}
    away = set()
    for unit in (p for p in paths() if p.endswith(".service")):
        for match in PROGRAM.finditer((ROOT / unit).read_text(encoding="utf-8")):
            program = names.get(PurePosixPath(match.group(1)).name)
            assert program, f"{unit} runs {match.group(1)}, which is not in git: moved?"
            folder, home = PurePosixPath(unit).parent, PurePosixPath(program).parent
            if folder not in (home, home / "systemd"):
                away.add(unit)
    return away


def test_a_unit_sits_beside_the_program_it_runs() -> None:
    """In its program's folder, or a systemd/ folder beside it, where the deploy finds a stack's."""
    away = units_away_from_their_program()
    assert away <= TODO_UNITS, "move the unit beside its program"
    assert not TODO_UNITS - away, "moved: take it off TODO_UNITS"


def path_pattern() -> re.Pattern[str]:
    """A repo path: a top-level folder, then more, and not the tail of a longer path."""
    tops = sorted({p.split("/")[0] for p in paths() if "/" in p} | FOLDERS.keys())
    either = "|".join(map(re.escape, tops))
    return re.compile(rf"(?<![\w./~%-])((?:{either})/[\w./-]*\w)")


def exists(path: str, named_in: str) -> bool:
    """A tracked file or folder, from the root or from the naming file's folder. A path with no
    extension may be cut short (`rules` for rules.yml, a glob's stem, an escaped regex)."""
    if path in HOST_ONLY:
        return True
    for candidate in (path, str(PurePosixPath(named_in).parent / path)):
        cut_short = "." not in PurePosixPath(candidate).name
        for p in paths():
            if (
                p == candidate
                or p.startswith(candidate + "/")
                or (cut_short and p.startswith(candidate))
            ):
                return True
    return False


@pytest.mark.parametrize(
    "ending",
    [
        ".md",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".service",
        ".timer",
        ".py",
        ".sh",
        ".alloy",
        "Caddyfile",
        "Corefile",
        "Dockerfile",
    ],
)
def test_each_path_named_still_exists(ending: str) -> None:
    """A move updates every workflow, unit, doc, alert and script that names the old path. Tests
    are left out: they name made-up paths on purpose."""
    pattern = path_pattern()
    stale = {
        (named_in, match.group(1))
        for named_in in paths()
        if named_in.endswith(ending) and not is_test_path(named_in)
        for match in pattern.finditer((ROOT / named_in).read_text(encoding="utf-8"))
        if not exists(match.group(1), named_in)
    }
    assert stale == set(), "change each to the path's new place"
