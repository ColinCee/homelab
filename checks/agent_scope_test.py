"""An agent's pull request merges itself only when it changes what runs no code on the Beelink
(checks/agent_scope.py)."""

from pathlib import Path

import pytest

from checks.agent_scope import outside
from conftest import commit, git


def _commit(repo: Path, files: list[str]) -> str:
    """Change each file, so every commit touches all of them."""
    sizes = {
        name: len((repo / name).read_text()) if (repo / name).exists() else 0 for name in files
    }
    return commit(repo, {name: f"{name} {size}\n" for name, size in sizes.items()})


@pytest.mark.parametrize(
    ("files", "held"),
    [
        (["stacks/observability/prometheus/rules.yml", "stacks/observability/rules_test.py"], []),
        (["stacks/observability/grafana/dashboards/disk.json", "docs/architecture.md"], []),
        (["README.md"], []),
        (["AGENTS.md"], ["AGENTS.md"]),
        (["stacks/observability/compose.yaml"], ["stacks/observability/compose.yaml"]),
        (["checks/ci.py", "docs/recovery.md"], ["checks/ci.py"]),
        ([".github/skills/homelab-oncall/SKILL.md"], [".github/skills/homelab-oncall/SKILL.md"]),
        (["stacks/observability/alertmanager/alertmanager.yml"],
         ["stacks/observability/alertmanager/alertmanager.yml"]),
    ],
)  # fmt: skip
def test_only_alert_rules_dashboards_docs_and_tests_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: list[str], held: list[str]
) -> None:
    git(tmp_path, "init", "-q")
    base = _commit(tmp_path, ["seed.txt"])
    head = _commit(tmp_path, files)
    monkeypatch.chdir(tmp_path)
    assert outside(base, head) == held


def test_a_rename_into_the_docs_is_held_by_its_old_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(tmp_path, "init", "-q")
    base = _commit(tmp_path, ["checks/ci.py"])
    (tmp_path / "docs").mkdir()
    git(tmp_path, "mv", "checks/ci.py", "docs/ci.md")
    git(tmp_path, "commit", "-qm", "mv")
    monkeypatch.chdir(tmp_path)
    assert outside(base, "HEAD") == ["checks/ci.py"]
