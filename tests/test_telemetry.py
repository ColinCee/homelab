"""Files read while they change are replaced only through telemetry.replace_file, and every program
importing the module can find it (stacks/telemetry/telemetry.py)."""

import re
import shlex
import stat
import sys
from pathlib import Path

import pytest
import telemetry

ROOT = Path(__file__).parent.parent
MODULE = ROOT / "stacks/telemetry/telemetry.py"
INSTALLED = "/usr/local/lib/homelab-ci/"
# The installer repairs homelab-python3, so it imports nothing from the checkout (install_host.py).
OWN_REPLACE = {MODULE, ROOT / "ci-runner/install_host.py"}


def test_files_are_replaced_only_through_telemetry() -> None:
    for path in [
        *ROOT.glob("scripts/**/*.py"),
        *ROOT.glob("stacks/**/*.py"),
        *ROOT.glob("ci-runner/**/*.py"),
    ]:
        if path not in OWN_REPLACE:
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"os\.(replace|rename)\(|\.rename\(", text), (
                f"{path.relative_to(ROOT)}: telemetry.replace_file"
            )


def imports_telemetry(script: Path) -> bool:
    return bool(re.search(r"^import telemetry$", script.read_text(encoding="utf-8"), re.MULTILINE))


def test_host_units_find_the_module() -> None:
    installer = (ROOT / "ci-runner/install_host.py").read_text(encoding="utf-8")
    checked = 0
    for unit in ROOT.glob("**/systemd/*.service"):
        text = unit.read_text(encoding="utf-8")
        for line in re.findall(r"^ExecStart=(.*\.py\b.*)$", text, re.MULTILINE):
            command = shlex.split(line)
            if command[0] == "/usr/bin/docker":
                continue  # In a container: test_json_logs checks its mount.
            script = next(part for part in command if part.endswith(".py"))
            name = Path(script).name
            source = ROOT / ("scripts" if script.startswith(INSTALLED) else "stacks/deploy") / name
            if not imports_telemetry(source):
                continue
            checked += 1
            # -I would drop the script's folder and PYTHONPATH from the path.
            assert "-I" not in command, unit.name
            if script.startswith(INSTALLED):
                assert "-Es" in command, unit.name
                assert 'here.parent / "stacks/telemetry/telemetry.py"' in installer
            else:
                assert "Environment=PYTHONPATH=%h/code/homelab/stacks/telemetry" in text, unit.name
    assert checked >= 5


def test_deploy_finds_the_module() -> None:
    assert imports_telemetry(ROOT / "scripts/deploy.py")
    wrapper = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
    assert 'PYTHONPATH="$repo_root/stacks/telemetry" exec python3 -s' in wrapper


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
@pytest.mark.parametrize("mode", [0o644, 0o600])
def test_a_replaced_file_has_its_mode_and_leaves_no_temporary(tmp_path: Path, mode: int) -> None:
    out = tmp_path / "x.prom"
    out.write_text("old\n")
    telemetry.replace_file(out, "new\n", mode)
    assert out.read_text() == "new\n"
    assert stat.S_IMODE(out.stat().st_mode) == mode
    assert [p.name for p in tmp_path.iterdir()] == ["x.prom"]


def test_a_failed_write_keeps_the_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def full(*_: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(telemetry.os, "replace", full)
    out = tmp_path / "x.prom"
    out.write_text("old\n")
    with pytest.raises(OSError):
        telemetry.replace_file(out, "new\n")
    assert out.read_text() == "old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["x.prom"]
