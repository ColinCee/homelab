"""Files read while they change are replaced only through telemetry.replace_file, and every program
importing the module can find it (stacks/telemetry/telemetry.py)."""

import re
import shlex
import stat
import subprocess
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
    return bool(re.search(r"^import telemetry\b", script.read_text(encoding="utf-8"), re.MULTILINE))


def test_installed_scripts_find_the_module_beside_them() -> None:
    installer = (ROOT / "ci-runner/install_host.py").read_text(encoding="utf-8")
    assert 'here.parent / "stacks/telemetry/telemetry.py"' in installer
    checked = 0
    for unit in ROOT.glob("ci-runner/systemd/*.service"):
        for line in re.findall(r"^ExecStart=(.*\.py\b.*)$", unit.read_text(encoding="utf-8"), re.M):
            command = shlex.split(line)
            script = next(part for part in command if part.endswith(".py"))
            if script.startswith(INSTALLED) and imports_telemetry(
                ROOT / "scripts" / Path(script).name
            ):
                checked += 1
                # -I would drop the script's folder, where the module is, from the path.
                assert "-Es" in command, unit.name
    assert checked >= 5


@pytest.mark.parametrize("script", ["scripts/deploy.py", "stacks/deploy/deploy_main.py"])
def test_checkout_scripts_find_the_module_under_any_unit(script: str, tmp_path: Path) -> None:
    # As the oldest deploy-main.service runs them: -I, no PYTHONPATH, run from elsewhere.
    load = f"import runpy; runpy.run_path({str(ROOT / script)!r}, run_name='loaded')"
    done = subprocess.run(
        [sys.executable, "-I", "-c", load], cwd=tmp_path, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr


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
