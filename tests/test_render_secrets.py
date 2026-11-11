from pathlib import Path

import pytest

from scripts.render_secrets import RenderError, render, secret_names

WORKFLOW = """\
jobs:
  deploy:
    steps:
      - name: Render secrets
        env:
          TOKEN: ${{ secrets.TOKEN }}
          PASSWORD: ${{ secrets.PASSWORD }}
          NOT_A_SECRET: ${{ github.sha }}
        run: scripts/render-secrets.sh
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/deploy.yaml").write_text(WORKFLOW, encoding="utf-8")
    return tmp_path


def test_the_workflows_render_step_is_the_list_of_secrets(repo: Path) -> None:
    assert secret_names(repo) == ("PASSWORD", "TOKEN")


def test_each_secret_is_its_raw_value_open_to_containers_but_not_host_users(repo: Path) -> None:
    written = render(repo, {"TOKEN": 'p$"#\\', "PASSWORD": "pw\n"})

    assert written == (repo / "secrets/password", repo / "secrets/token")
    assert (repo / "secrets/token").read_text() == 'p$"#\\'
    assert (repo / "secrets/password").read_text() == "pw"
    assert (repo / "secrets/token").stat().st_mode & 0o777 == 0o444
    assert (repo / "secrets").stat().st_mode & 0o777 == 0o700


def test_an_unchanged_secret_keeps_its_file_so_its_readers_are_not_recreated(repo: Path) -> None:
    render(repo, {"TOKEN": "a", "PASSWORD": "b"})
    first = (repo / "secrets/token").stat().st_ino

    render(repo, {"TOKEN": "a", "PASSWORD": "b"})
    assert (repo / "secrets/token").stat().st_ino == first

    render(repo, {"TOKEN": "c", "PASSWORD": "b"})
    assert (repo / "secrets/token").read_text() == "c"


def test_a_missing_secret_writes_nothing(repo: Path) -> None:
    with pytest.raises(RenderError, match="PASSWORD"):
        render(repo, {"TOKEN": "a"})

    assert not (repo / "secrets").exists()
