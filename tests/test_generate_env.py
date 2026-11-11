from pathlib import Path

import pytest

from scripts.generate_env import EnvGenerationError, generate_env_files, render_template


def test_render_preserves_quotes_backslashes_newlines_and_dollar_signs() -> None:
    secret = "p'a\\ss\nnext ${OTHER} $VALUE #"

    rendered = render_template("PASSWORD=${SECRET}\n", {"SECRET": secret})

    assert rendered == 'PASSWORD="p\'a\\\\ss\\nnext $${OTHER} $$VALUE #"\n'


def test_render_handles_trailing_backslashes_and_double_quotes() -> None:
    secret = 'ends\\ and "quoted"\\'

    rendered = render_template("VALUE=${VALUE}\n", {"VALUE": secret})

    assert rendered == 'VALUE="ends\\\\ and \\"quoted\\"\\\\"\n'


def test_missing_inputs_are_checked_before_any_output_is_replaced(tmp_path: Path) -> None:
    stacks = tmp_path / "stacks"
    first = stacks / "first"
    second = stacks / "second"
    first.mkdir(parents=True)
    second.mkdir()
    (first / ".env.example").write_text("FIRST=${FIRST}\n", encoding="utf-8")
    (second / ".env.example").write_text("SECOND=${SECOND}\n", encoding="utf-8")
    existing = first / ".env"
    existing.write_text("old\n", encoding="utf-8")

    with pytest.raises(EnvGenerationError, match=r"second: .*SECOND"):
        generate_env_files(tmp_path, ("first", "second"), environment={"FIRST": "new"})

    assert existing.read_text() == "old\n"
    assert not (second / ".env").exists()


def test_generation_is_atomic_and_restricts_file_mode(tmp_path: Path) -> None:
    stack = tmp_path / "stacks" / "sample"
    stack.mkdir(parents=True)
    (stack / ".env.example").write_text(
        "# keep comments\nPASSWORD=${PASSWORD}\nconstant=value\n", encoding="utf-8"
    )

    generated = generate_env_files(tmp_path, ("sample",), environment={"PASSWORD": "secret"})

    assert generated == (stack / ".env",)
    assert (stack / ".env").read_text() == (
        '# keep comments\nPASSWORD="secret"\nconstant="value"\n'
    )
    assert (stack / ".env").stat().st_mode & 0o777 == 0o600


def test_unknown_stack_does_not_replace_already_planned_files(tmp_path: Path) -> None:
    stack = tmp_path / "stacks" / "known"
    stack.mkdir(parents=True)
    (stack / ".env.example").write_text("VALUE=new\n")
    (stack / ".env").write_text("VALUE=old\n")

    with pytest.raises(EnvGenerationError, match="Stack directory not found"):
        generate_env_files(tmp_path, ("known", "typo"), environment={})

    assert (stack / ".env").read_text() == "VALUE=old\n"


def test_secret_files_hold_the_raw_value_open_to_containers_but_not_host_users(
    tmp_path: Path,
) -> None:
    secrets = tmp_path / "stacks" / "sample" / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "login.example").write_text("user:${PASSWORD}\n", encoding="utf-8")

    generated = generate_env_files(tmp_path, None, environment={"PASSWORD": 'p$"#'})

    assert generated == (secrets / "login",)
    assert (secrets / "login").read_text() == 'user:p$"#'
    assert (secrets / "login").stat().st_mode & 0o777 == 0o444
    assert secrets.stat().st_mode & 0o777 == 0o700


def test_an_unchanged_secret_keeps_its_file_so_its_readers_are_not_recreated(
    tmp_path: Path,
) -> None:
    secrets = tmp_path / "stacks" / "sample" / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "token.example").write_text("${TOKEN}", encoding="utf-8")
    generate_env_files(tmp_path, ("sample",), environment={"TOKEN": "a"})
    first = (secrets / "token").stat().st_ino

    generate_env_files(tmp_path, ("sample",), environment={"TOKEN": "a"})
    assert (secrets / "token").stat().st_ino == first

    generate_env_files(tmp_path, ("sample",), environment={"TOKEN": "b"})
    assert (secrets / "token").read_text() == "b"


def test_a_missing_secret_input_replaces_nothing(tmp_path: Path) -> None:
    stack = tmp_path / "stacks" / "sample"
    (stack / "secrets").mkdir(parents=True)
    (stack / ".env.example").write_text("A=${A}\n", encoding="utf-8")
    (stack / "secrets" / "token.example").write_text("${TOKEN}", encoding="utf-8")

    with pytest.raises(EnvGenerationError, match="TOKEN"):
        generate_env_files(tmp_path, ("sample",), environment={"A": "a"})

    assert not (stack / ".env").exists()


def test_a_stack_whose_settings_all_became_secret_files_loses_its_old_env(tmp_path: Path) -> None:
    stack = tmp_path / "stacks" / "sample"
    (stack / "secrets").mkdir(parents=True)
    (stack / "secrets" / "token.example").write_text("${TOKEN}", encoding="utf-8")
    (stack / ".env").write_text('TOKEN="old"\n', encoding="utf-8")

    generate_env_files(tmp_path, None, environment={"TOKEN": "a"})

    assert not (stack / ".env").exists()
