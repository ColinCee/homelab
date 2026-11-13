"""An agent's pull request merges itself only when it changes what runs no code on the Beelink
(scripts/agent_scope.py)."""

import subprocess
from pathlib import Path

import pytest

from scripts.agent_scope import outside


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, files: list[str]) -> str:
    for name in files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{name} {len(path.read_text()) if path.exists() else 0}\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    return _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    ("files", "held"),
    [
        (["stacks/observability/prometheus/rules.yml", "tests/test_alert_rules.py"], []),
        (["stacks/observability/grafana/dashboards/disk.json", "docs/architecture.md"], []),
        (["README.md"], []),
        (["AGENTS.md"], ["AGENTS.md"]),
        (["stacks/observability/compose.yaml"], ["stacks/observability/compose.yaml"]),
        (["scripts/ci.py", "docs/recovery.md"], ["scripts/ci.py"]),
        ([".github/skills/homelab-oncall/SKILL.md"], [".github/skills/homelab-oncall/SKILL.md"]),
        (["stacks/observability/alertmanager/alertmanager.yml"],
         ["stacks/observability/alertmanager/alertmanager.yml"]),
    ],
)  # fmt: skip
def test_only_alert_rules_dashboards_docs_and_tests_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: list[str], held: list[str]
) -> None:
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, ["seed.txt"])
    head = _commit(tmp_path, files)
    monkeypatch.chdir(tmp_path)
    assert outside(base, head) == held


def test_a_rename_into_the_docs_is_held_by_its_old_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, ["scripts/ci.py"])
    (tmp_path / "docs").mkdir()
    _git(tmp_path, "mv", "scripts/ci.py", "docs/ci.md")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "mv")
    monkeypatch.chdir(tmp_path)
    assert outside(base, "HEAD") == ["scripts/ci.py"]
