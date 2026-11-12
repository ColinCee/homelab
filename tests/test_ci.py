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
    for name in ci.CACHE_INPUTS:
        (tmp_path / name).write_text(name, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ci, "CACHE_KEY", tmp_path / "cache" / "mise" / ".ci-key")
    out = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    ci.cache_changed()
    assert out.read_text() == "changed=true\n"
    out.write_text("")
    ci.cache_changed()
    assert out.read_text() == ""
    (tmp_path / "uv.lock").write_text("new", encoding="utf-8")
    ci.cache_changed()
    assert out.read_text() == "changed=true\n"


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


def test_the_registry_login_stays_in_the_job_s_temp_folder_and_takes_the_token_on_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(
        ci.subprocess,
        "run",
        lambda command, **kw: calls.append((command, kw)) or type("R", (), {"returncode": 0})(),
    )
    for name, value in [("RUNNER_TEMP", str(tmp_path)), ("GITHUB_ENV", str(tmp_path / "env")),
                        ("GHCR_USER", "ColinCee"), ("GHCR_TOKEN", "t0ken")]:  # fmt: skip
        monkeypatch.setenv(name, value)
    assert ci.ghcr_login() == 0
    [(command, kw)] = calls
    assert "t0ken" not in command and kw["input"] == "t0ken"
    assert kw["env"]["DOCKER_CONFIG"] == str(tmp_path / "docker")
    assert (tmp_path / "env").read_text() == f"DOCKER_CONFIG={tmp_path / 'docker'}\n"
