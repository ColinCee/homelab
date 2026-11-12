"""Installing the CI host (ci-runner/install_host.py): what restarts when, and what goes where."""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CI_RUNNER = Path(__file__).parent.parent / "ci-runner"
_spec = importlib.util.spec_from_file_location("install_host", CI_RUNNER / "install_host.py")
assert _spec is not None and _spec.loader is not None
ih = importlib.util.module_from_spec(_spec)
sys.modules["install_host"] = ih
_spec.loader.exec_module(ih)

VERSION = ih.versions((CI_RUNNER / "versions.env").read_text())["FIRECRACKER_VERSION"]
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
            case ["id", name]:
                code = 0 if name in self.users else 1
            case ["useradd", *_, name]:
                self.users.add(name)
            case ["install", "-d", "-o", "root", "-g", "root", "-m", mode, *dirs]:
                for d in map(Path, dirs):
                    d.mkdir(parents=True, exist_ok=True)
                    d.chmod(int(mode, 8))
            case ["install", *_, "-m", mode, src, dst] if Path(src).exists():
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
    return tmp_path


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
    # A break in scripts/ must not stop the installer that would install its fix.
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
