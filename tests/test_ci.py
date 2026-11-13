"""The CI workflows' steps (scripts/ci.py): the rules they apply and what they tell later steps."""

from pathlib import Path

import pytest

from scripts import ci

ROOT = Path(__file__).parent.parent


def conftest_with(run: float | None, test: float | None) -> str:
    lines = [
        f"RUN_LIMIT = {run}" if run is not None else "",
        f"TEST_LIMIT = {test}" if test else "",
    ]
    return "\n".join(["import os", *lines, ""])


@pytest.mark.parametrize(
    ("head", "raised"),
    [
        (conftest_with(30.0, 5.0), []),
        (conftest_with(20.0, 4.0), []),  # lowered
        (conftest_with(40.0, 5.0), ["RUN_LIMIT 30 s to 40 s"]),
        (conftest_with(None, 5.0), ["RUN_LIMIT 30 s to none"]),  # removed
    ],
)
def test_a_raised_or_removed_time_limit_is_named(head: str, raised: list[str]) -> None:
    assert ci.raised_limits(conftest_with(30.0, 5.0), head) == raised


def test_a_base_without_limits_judges_nothing_and_the_real_limits_parse() -> None:
    assert ci.raised_limits(None, conftest_with(99.0, 99.0)) == []
    real = ci.limits((ROOT / ci.LIMITS_FILE).read_text(encoding="utf-8"))
    assert set(real) == set(ci.LIMIT_NAMES)


def test_the_slower_ci_label_is_read_from_the_pull_request() -> None:
    event = {"pull_request": {"labels": [{"name": "bug"}, {"name": ci.SLOWER_LABEL}]}}
    assert ci.SLOWER_LABEL in ci.labels(event)
    assert ci.labels({}) == []


def test_versions_env_reads_as_the_shell_does() -> None:
    text = 'A_VERSION=1.2 # pinned\n# B_VERSION=9\nC_LIST="one\n  two"\nlower=x\n'
    assert ci.versions(text) == {"A_VERSION": "1.2", "C_LIST": "one\n  two"}
    real = ci.versions((ROOT / ci.VERSIONS).read_text(encoding="utf-8"))
    assert "RUNNER_VERSION" in real and "@" in real["CACHED_ACTIONS"]


def test_an_annotation_is_one_line_with_percent_escaped() -> None:
    assert ci.annotation('[["a%b",1]]\n') == '[["a%25b",1]]%0A'


def test_the_cache_disk_is_saved_only_when_its_inputs_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mise = tmp_path / "mise.toml"
    mise.write_text('[tools]\nuv = "1"\n\n[tasks.lint]\nrun = "a"\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text("lock", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ci, "CACHE_KEY", tmp_path / "cache" / "mise" / ".ci-key")
    out = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    def saves() -> bool:
        out.write_text("")
        ci.cache_changed()
        return out.read_text() == "changed=true\n"

    assert saves() and not saves()
    # A task changes nothing the cache holds; a tool or a locked package does.
    mise.write_text('[tools]\nuv = "1"\n\n[tasks.lint]\nrun = "b"\n', encoding="utf-8")
    assert not saves()
    mise.write_text('[tools]\nuv = "2"\n\n[tasks.lint]\nrun = "b"\n', encoding="utf-8")
    assert saves()
    (tmp_path / "uv.lock").write_text("new", encoding="utf-8")
    assert saves()


def test_only_a_pin_only_change_tells_the_next_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    # No rule in the base (None) means full checks, never a skip.
    for verdict, said in [(1, ""), (None, ""), (0, "only=true\n")]:
        out.write_text("")
        monkeypatch.setattr(ci, "run_base_rule", lambda script, v=verdict: v)
        ci.pins_only()
        assert out.read_text() == said


def test_the_pull_request_rules_all_run_and_any_failure_fails_the_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []
    names = ["pins_only", "versions_hash", "time_limits", "agent_scope"]
    for name, verdict in zip(names, [0, 1, 0, 0], strict=True):
        monkeypatch.setattr(ci, name, lambda n=name, v=verdict: ran.append(n) or v)
    assert ci.pull_request_rules() == 1
    assert ran == names


def test_only_the_on_call_agents_branch_is_held_to_its_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ci, "run_base_rule", lambda script: 1)
    monkeypatch.setenv("GITHUB_HEAD_REF", "claude/fix-disk-alert")
    assert ci.agent_scope() == 0
    monkeypatch.setenv("GITHUB_HEAD_REF", ci.ONCALL_BRANCH)
    assert ci.agent_scope() == 1
