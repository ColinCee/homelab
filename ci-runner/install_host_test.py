"""Installing the CI host (ci-runner/install_host.py): what restarts when, and what goes where."""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from conftest import ROOT, stacks

CI_RUNNER = ROOT / "ci-runner"
_spec = importlib.util.spec_from_file_location("install_host", CI_RUNNER / "install_host.py")
assert _spec is not None and _spec.loader is not None
ih = importlib.util.module_from_spec(_spec)
sys.modules["install_host"] = ih
_spec.loader.exec_module(ih)

PINNED = ih.versions((CI_RUNNER / "versions.env").read_text())
VERSION = PINNED["FIRECRACKER_VERSION"]
PYTHON_NAME = f"{PINNED['PBS_PYTHON']}+{PINNED['PBS_VERSION']}"
PYTHON_TARGET = f"{ih.PYTHON_DIR}/{PYTHON_NAME}/bin/python3"
SERVICE = ih.SERVICE


class Recorder:
    """Records each command; acts out `install` in the tmp root, and systemd and the binaries'
    answers as a host would give them."""

    def __init__(self, *, active: bool, version: str = VERSION) -> None:
        self.calls: list[list[str]] = []
        self.active = active
        self.version = version
        self.users: set[str] = set()
        self.fail: str | None = None
        self.smoke_fails = False
        self.fail_program: str | None = None
        self.link_when_first_unit_written: bool | None = None

    def __call__(
        self,
        command: list[str],
        *,
        check: bool = True,
        capture: bool = False,
        quiet: bool = False,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        code, out = 0, ""
        match command:
            case [program, *_] if program == self.fail_program:
                code = 1
            case [*_, name] if self.fail and name == self.fail:
                code = 1
            case [path, "up"] if path == self.fail:
                code = 1
            case ["systemctl", "is-active", "--quiet", _]:
                code = 0 if self.active else 3
            case ["systemctl", "stop", unit] if unit == SERVICE:
                self.active = False
            case ["systemctl", "enable" | "start", *_, unit] if unit == SERVICE:
                self.active = True
            case [path, "--version"]:
                name = Path(path).name.capitalize()
                out = f"{name} {self.version}\n" if self.version else ""
                code = 0 if self.version else 127
            case ["curl", *_, tgz, _]:
                Path(tgz).write_bytes(b"tarball")
            case ["tar", "-xzf", _, "-C", tmp, *_]:
                (Path(tmp) / "python/bin").mkdir(parents=True)
                (Path(tmp) / "python/bin/python3").write_text("python")
            case [*_, python, "-c", ih.PYTHON_SMOKE]:
                code = 127 if not Path(python).exists() else int(self.smoke_fails)
            case ["id", name]:
                code = 0 if name in self.users else 1
            case ["useradd", *_, name]:
                self.users.add(name)
            case ["install", "-d", "-o", "root", "-g", "root", "-m", mode, *dirs]:
                for d in map(Path, dirs):
                    d.mkdir(parents=True, exist_ok=True)
                    d.chmod(int(mode, 8))
            case ["install", *_, "-m", mode, src, dst] if Path(src).exists():
                if "/etc/systemd/system/" in dst and self.link_when_first_unit_written is None:
                    link = Path(dst.split("/etc/systemd")[0]) / "usr/local/bin/homelab-python3"
                    self.link_when_first_unit_written = link.is_symlink()
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                Path(dst).unlink(missing_ok=True)
                shutil.copyfile(src, dst)
                Path(dst).chmod(int(mode, 8))
        if check and code:
            raise subprocess.CalledProcessError(code, command)
        return subprocess.CompletedProcess(command, code, out, "")

    def ran(self, *command: str) -> bool:
        return list(command) in self.calls

    def installed(self, dst: Path) -> bool:
        return any(c[0] == "install" and c[-1] == str(dst) for c in self.calls)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "sys/kernel/mm/ksm").mkdir(parents=True)
    (tmp_path / "etc/tmpfiles.d").mkdir(parents=True)
    # The pinned Python is already there; the tests of installing it start from `link_to`.
    (tmp_path / "usr/local/bin").mkdir(parents=True)
    (tmp_path / "usr/local/bin/homelab-python3").symlink_to(PYTHON_TARGET)
    built = tmp_path / PYTHON_TARGET.lstrip("/")
    built.parent.mkdir(parents=True)
    built.write_text("python")
    return tmp_path


def link_to(root: Path, target: str) -> Path:
    """A host running an older Python: its build directory and the link to it."""
    link = root / "usr/local/bin/homelab-python3"
    link.unlink()
    link.symlink_to(target)
    (root / target.lstrip("/")).parent.parent.mkdir(parents=True, exist_ok=True)
    return link


def install(root: Path, recorder: Recorder, **env: str) -> None:
    ih.install_host(ih.Host(root=root, run=recorder), CI_RUNNER, env)


def installed_host(root: Path) -> Recorder:
    """A host after its first install, with the supervisor started by hand."""
    install(root, Recorder(active=False))
    return Recorder(active=True)


def installed_at(recorder: Recorder, dst: Path) -> int:
    return next(i for i, c in enumerate(recorder.calls) if c[-1] == str(dst))


def restarted(recorder: Recorder) -> bool:
    return recorder.ran("systemctl", "stop", SERVICE)


def test_a_first_install_sets_up_the_network_but_starts_no_supervisor(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = Recorder(active=False)

    install(root, recorder)

    dest = root / "usr/local/lib/homelab-ci"
    assert recorder.ran(str(dest / "net/setup-net.sh"), "up")
    assert not recorder.ran("systemctl", "enable", "--now", SERVICE)
    assert not recorder.active
    assert (root / "etc/homelab-ci/slots").read_text() == "5\n"
    assert (root / "etc/homelab-ci").stat().st_mode & 0o777 == 0o700
    assert (dest / "ci_supervisor.py").stat().st_mode & 0o777 == 0o755
    assert (dest / "test_load.py").stat().st_mode & 0o777 == 0o755
    # It runs from there (ci_supervisor.py, LOAD_TEST_SCRIPT) with only what is installed beside it.
    found = subprocess.run(
        [sys.executable, "-Es", "-c", "import test_load"], cwd=dest, capture_output=True, text=True
    )
    assert found.returncode == 0, found.stderr
    assert (dest / "net/setup_net.py").stat().st_mode & 0o777 == 0o755
    for name in ("vm_disks.py", "run_vm.py", "stop_vms.py"):
        assert (dest / name).stat().st_mode & 0o777 == 0o644
    assert (dest / "update.py").read_bytes() == (CI_RUNNER / "update.py").read_bytes()
    assert (root / "sys/kernel/mm/ksm/run").read_text() == "0\n"
    assert {f"fc-slot{n}" for n in range(5)} | {"homelab-facts"} == recorder.users
    assert not (root / "run/homelab-ci/restart-reason").exists()
    assert capsys.readouterr().out.endswith(
        f"Firecracker {VERSION}, 5 slots, network up, supervisor not running.\n"
    )


def test_the_old_vm_disks_script_goes_only_after_the_wrappers_that_no_longer_need_it(
    root: Path,
) -> None:
    recorder = Recorder(active=False)
    stale = root / "usr/local/lib/homelab-ci/vm-disks.sh"
    stale.parent.mkdir(parents=True)
    stale.write_text("old")
    stale_at_wrapper_install: list[bool] = []

    def watching(
        command: list[str],
        *,
        check: bool = True,
        capture: bool = False,
        quiet: bool = False,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if command[0] == "install" and command[-1] == str(stale.with_name("run-vm.sh")):
            stale_at_wrapper_install.append(stale.exists())
        return recorder(command, check=check, capture=capture, quiet=quiet, input=input)

    ih.install_host(ih.Host(root=root, run=watching), CI_RUNNER, {})

    assert stale_at_wrapper_install == [True]
    assert not stale.exists()


def test_unchanged_supervisor_and_network_swap_vm_files_without_a_restart(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = installed_host(root)
    capsys.readouterr()

    stale = root / "usr/local/lib/homelab-ci/vm-disks.sh"
    stale.write_text("old")
    install(root, recorder)

    assert not restarted(recorder)
    assert not any(c[-1] == "up" for c in recorder.calls)
    dest = root / "usr/local/lib/homelab-ci"
    assert recorder.installed(dest / "run-vm.sh")
    assert not (dest / "vm-disks.sh").exists()
    # Each wrapper is installed after the Python it runs.
    for py, sh in (("run_vm.py", "run-vm.sh"), ("stop_vms.py", "stop-vms.sh")):
        assert installed_at(recorder, dest / py) < installed_at(recorder, dest / sh)
    # The wrapper the timer starts is never installed before the file it runs.
    assert installed_at(recorder, dest / "update.py") < installed_at(recorder, dest / "update.sh")
    assert "installed while jobs ran, no restart needed." in capsys.readouterr().out


@pytest.mark.parametrize(
    "changed",
    [
        "usr/local/lib/homelab-ci/ci_supervisor.py",
        "usr/local/lib/homelab-ci/net/homelab-ci.nft",
        "usr/local/lib/homelab-ci/net/setup-net.sh",
        "usr/local/lib/homelab-ci/net/setup_net.py",
        f"etc/systemd/system/{SERVICE}",
        "etc/systemd/system/homelab-ci-net.service",
    ],
)
def test_a_changed_supervisor_or_network_file_restarts_the_supervisor(
    root: Path, changed: str
) -> None:
    recorder = installed_host(root)
    (root / changed).write_text("old UNIT_PREFIX\n")

    install(root, recorder)

    assert restarted(recorder)
    assert recorder.ran(str(root / "usr/local/lib/homelab-ci/net/setup-net.sh"), "up")
    assert recorder.ran("systemctl", "enable", "--now", SERVICE)
    assert (root / changed).read_bytes() != b"old UNIT_PREFIX\n"


def test_the_network_code_is_installed_before_its_wrapper(root: Path) -> None:
    recorder = installed_host(root)
    (root / "usr/local/lib/homelab-ci/net/setup_net.py").write_text("old\n")

    install(root, recorder)

    dest = root / "usr/local/lib/homelab-ci/net"
    order = [
        c[-1]
        for c in recorder.calls
        if c[-1] in (str(dest / "setup_net.py"), str(dest / "setup-net.sh"))
    ]
    assert order == [str(dest / "setup_net.py"), str(dest / "setup-net.sh")]


def test_a_new_slot_count_restarts_and_adds_its_users(root: Path) -> None:
    recorder = installed_host(root)
    recorder.users = {f"fc-slot{n}" for n in range(5)}

    install(root, recorder, SLOTS="7")

    assert restarted(recorder)
    assert (root / "etc/homelab-ci/slots").read_text() == "7\n"
    assert recorder.users >= {"fc-slot5", "fc-slot6"}


def test_the_slot_count_comes_from_the_host_file_when_not_given(root: Path) -> None:
    recorder = installed_host(root)
    (root / "etc/homelab-ci/slots").write_text("3\n")

    install(root, recorder)

    assert not restarted(recorder)


@pytest.mark.parametrize(("given", "slots"), [("0", 1), ("x", 5), (" 0 4\n", 4), ("", 5)])
def test_the_slot_count_is_at_least_one_and_five_when_not_a_number(
    root: Path, given: str, slots: int
) -> None:
    (root / "etc/homelab-ci").mkdir(parents=True)
    (root / "etc/homelab-ci/slots").write_text(given)

    assert ih.slot_count({}, ih.Host(root=root)) == slots
    assert ih.slot_count({"SLOTS": "2"}, ih.Host(root=root)) == 2


def test_a_stopped_supervisor_restarts_the_network_but_stays_stopped(root: Path) -> None:
    recorder = installed_host(root)
    recorder.active = False

    install(root, recorder)

    assert recorder.ran(str(root / "usr/local/lib/homelab-ci/net/setup-net.sh"), "up")
    assert not recorder.ran("systemctl", "stop", SERVICE)
    assert not recorder.ran("systemctl", "enable", "--now", SERVICE)
    assert not recorder.ran("systemctl", "start", SERVICE)


def test_a_failed_install_starts_the_supervisor_it_stopped(root: Path) -> None:
    recorder = installed_host(root)
    (root / "usr/local/lib/homelab-ci/ci_supervisor.py").write_text("old\n")
    recorder.fail = str(root / "usr/local/lib/homelab-ci/net/setup-net.sh")

    with pytest.raises(subprocess.CalledProcessError):
        install(root, recorder, REASON="homelab #1")

    assert recorder.calls[-1] == ["systemctl", "start", SERVICE]
    assert recorder.active
    assert not (root / "run/homelab-ci/restart-reason").exists()


def test_firecracker_is_downloaded_only_when_its_version_differs(root: Path) -> None:
    recorder = Recorder(active=False)
    install(root, recorder)
    assert not any(c[0] == "curl" for c in recorder.calls)

    recorder = Recorder(active=False, version="v0.1.0")
    install(root, recorder)
    curl = next(c for c in recorder.calls if c[0] == "curl")
    assert curl[-1].endswith(f"/{VERSION}/firecracker-{VERSION}-x86_64.tgz")
    assert recorder.installed(root / "usr/local/bin/firecracker")
    assert recorder.installed(root / "usr/local/bin/jailer")


def test_a_bad_firecracker_download_stops_before_installing_it(root: Path) -> None:
    recorder = Recorder(active=False, version="")
    recorder.fail = "--quiet"  # sha256sum -c --quiet

    with pytest.raises(subprocess.CalledProcessError):
        install(root, recorder)

    assert not recorder.installed(root / "usr/local/bin/firecracker")
    assert not any(c[0] == "useradd" for c in recorder.calls)


def test_the_installer_imports_nothing_from_the_checkout(tmp_path: Path) -> None:
    # A break in the code it installs must not stop the installer that would install its fix.
    shutil.copy(CI_RUNNER / "install_host.py", tmp_path / "install_host.py")
    load = f"import sys; sys.path.insert(0, {str(tmp_path)!r}); import install_host"

    result = subprocess.run(
        [sys.executable, "-I", "-c", load], cwd=tmp_path, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    if os.geteuid() != 0:
        result = subprocess.run(
            [sys.executable, "-I", str(tmp_path / "install_host.py")],
            capture_output=True,
            text=True,
        )
        assert (result.returncode, result.stderr) == (1, "run with sudo\n")


OLD_TARGET = f"{ih.PYTHON_DIR}/3.14.0+20250101/bin/python3"


def test_every_python_image_is_the_version_mise_pins() -> None:
    root = CI_RUNNER.parent
    python = tomllib.loads((root / "mise.toml").read_text())["tools"]["python"]
    files = [*root.glob("stacks/**/*.Dockerfile"), *(stack / "compose.yaml" for stack in stacks())]
    tags = {
        (file.name, tag)
        for file in files
        for tag in re.findall(r"python:(\d+\.\d+\.\d+)", file.read_text())
    }
    assert tags
    assert {tag for _, tag in tags} == {python}, tags


def test_the_host_python_is_the_one_mise_pins() -> None:
    mise = tomllib.loads((CI_RUNNER.parent / "mise.toml").read_text())
    assert PINNED["PBS_PYTHON"] == mise["tools"]["python"]


def test_an_unchanged_python_is_not_downloaded(root: Path) -> None:
    recorder = Recorder(active=False)
    install(root, recorder)
    assert not any(c[0] in ("curl", "tar") for c in recorder.calls)


def test_a_new_python_moves_the_link_before_the_units_are_written(root: Path) -> None:
    link = link_to(root, OLD_TARGET)
    (root / ih.PYTHON_DIR.lstrip("/") / "3.13.0+20240101").mkdir(parents=True)
    recorder = Recorder(active=False)

    install(root, recorder)

    curl = next(c for c in recorder.calls if c[0] == "curl")
    tag, python = PINNED["PBS_VERSION"], PINNED["PBS_PYTHON"]
    assert curl[-1].endswith(
        f"/{tag}/cpython-{python}%2B{tag}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
    )
    assert os.readlink(link) == PYTHON_TARGET
    assert recorder.link_when_first_unit_written is True
    homes = sorted(p.name for p in (root / ih.PYTHON_DIR.lstrip("/")).iterdir())
    assert homes == sorted(["3.14.0+20250101", PYTHON_NAME])


def test_a_python_download_with_the_wrong_hash_changes_nothing(root: Path) -> None:
    link = link_to(root, OLD_TARGET)
    recorder = Recorder(active=True)
    recorder.fail = "--quiet"  # sha256sum -c --quiet

    with pytest.raises(subprocess.CalledProcessError):
        install(root, recorder)

    assert os.readlink(link) == OLD_TARGET
    assert recorder.link_when_first_unit_written is None
    assert not any(c[0] == "tar" for c in recorder.calls)
    assert not restarted(recorder)


@pytest.mark.parametrize("program", ["curl", "tar"])
def test_a_failed_python_download_or_extraction_changes_nothing(root: Path, program: str) -> None:
    link = link_to(root, OLD_TARGET)
    recorder = Recorder(active=True)
    recorder.fail_program = program

    with pytest.raises(subprocess.CalledProcessError):
        install(root, recorder)

    assert os.readlink(link) == OLD_TARGET
    assert recorder.link_when_first_unit_written is None
    assert not restarted(recorder)


def test_a_first_install_with_no_link_downloads_and_links_the_python(root: Path) -> None:
    link = root / "usr/local/bin/homelab-python3"
    link.unlink()
    recorder = Recorder(active=False)

    install(root, recorder)

    assert any(c[0] == "curl" for c in recorder.calls)
    assert os.readlink(link) == PYTHON_TARGET
    assert recorder.link_when_first_unit_written is True


def test_a_link_to_a_python_that_is_gone_is_reinstalled(root: Path) -> None:
    (root / PYTHON_TARGET.lstrip("/")).unlink()
    recorder = Recorder(active=False)

    install(root, recorder)

    assert any(c[0] == "curl" for c in recorder.calls)
    assert (root / PYTHON_TARGET.lstrip("/")).exists()


def test_a_python_that_fails_its_smoke_test_is_not_linked(root: Path) -> None:
    link = link_to(root, OLD_TARGET)
    recorder = Recorder(active=True)
    recorder.smoke_fails = True

    with pytest.raises(subprocess.CalledProcessError):
        install(root, recorder)

    assert os.readlink(link) == OLD_TARGET
    assert recorder.link_when_first_unit_written is None
    assert not restarted(recorder)
