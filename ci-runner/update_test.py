"""Bringing the CI host to a commit of main (ci-runner/update.py), against real git repos in a tmp
root: what is installed and rebuilt when, giving up, and the commit marker written last."""

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from conftest import GIT, ROOT, git, units

CI_RUNNER = ROOT / "ci-runner"
_spec = importlib.util.spec_from_file_location("ci_update", CI_RUNNER / "update.py")
assert _spec is not None and _spec.loader is not None
up = importlib.util.module_from_spec(_spec)
sys.modules["ci_update"] = up
_spec.loader.exec_module(up)


def commit(repo: Path, path: str, message: str) -> str:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(f"{message}\n")
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


class Host:
    """A tmp root with a checkout of a local main, git run for real, the rest recorded."""

    def __init__(self, tmp: Path) -> None:
        self.root = tmp / "root"
        self.origin = tmp / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q")
        self.first = commit(self.origin, "README.md", "start")
        for d in ("etc/homelab-ci", "var/lib/homelab-ci/images", "run"):
            (self.root / d).mkdir(parents=True)
        subprocess.run([*GIT, "clone", "-q", str(self.origin), str(self.src)], check=True)
        self.calls: list[list[str]] = []
        self.codes: dict[str, int] = {}
        # The installed commit each install or build saw while it ran.
        self.seen: list[str] = []

    @property
    def src(self) -> Path:
        return self.root / "var/lib/homelab-ci/src"

    @property
    def installed(self) -> str:
        path = self.root / "etc/homelab-ci/commit"
        return path.read_text().strip() if path.exists() else ""

    @property
    def built(self) -> str:
        path = self.root / "var/lib/homelab-ci/images/commit"
        return path.read_text().strip() if path.exists() else ""

    def at(self, have: str, built: str | None = None) -> None:
        git(self.src, "fetch", "-q", "origin", "main")
        (self.root / "etc/homelab-ci/commit").write_text(f"{have}\n")
        (self.root / "var/lib/homelab-ci/images/commit").write_text(f"{built or have}\n")

    def run(
        self,
        command: list[str],
        *,
        check: bool = True,
        capture: bool = False,
        quiet: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if command[0] == "git":
            return subprocess.run(
                command,
                check=check,
                text=True,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        self.calls.append(command)
        name = Path(command[-1]).name if command[0] != "timeout" else "ls-remote"
        if name in ("install-host.sh", "build-rootfs.sh"):
            self.seen.append(self.installed)
        if name == "install-host.sh":
            self.calls[-1] = [*command, f"REASON={(env or {}).get('REASON')}"]
        out = git(self.origin, "rev-parse", "HEAD").strip() + "\trefs/heads/main\n"
        code = self.codes.get(name, 0)
        if check and code:
            raise subprocess.CalledProcessError(code, command)
        return subprocess.CompletedProcess(command, code, out if name == "ls-remote" else "", "")

    def update(self, *args: str, now: float | None = None, **env: str) -> int:
        host = up.Host(root=self.root, run=self.run)
        return int(up.update(host, list(args), env, time.time() if now is None else now))

    def ran(self, name: str) -> list[list[str]]:
        return [c for c in self.calls if any(Path(part).name == name for part in c)]


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


def test_a_change_outside_the_host_code_installs_nothing(host: Host) -> None:
    host.at(host.first)
    want = commit(host.origin, "stacks/x/compose.yaml", "a stack (#9)")

    assert host.update(want) == 0

    assert host.ran("install-host.sh") == []
    assert host.ran("build-rootfs.sh") == []
    assert host.installed == want


def test_a_change_to_the_host_code_installs_with_its_pull_requests_as_the_reason(
    host: Host,
) -> None:
    host.at(host.first)
    commit(host.origin, "ci-runner/run-times/ci_times.py", "Times (#12)")
    commit(host.origin, "stacks/x/compose.yaml", "A stack (#13)")
    want = commit(host.origin, "ci-runner/supervisor/run-vm.sh", "VMs; rm -rf / `x` (#14)")

    assert host.update(want) == 0

    [install] = host.ran("install-host.sh")
    assert install[-1] == "REASON=homelab #14 #12"
    assert [c[:3] for c in host.ran("logger")] == [["logger", "-t", "homelab-ci-update"]]
    assert host.ran("logger")[0][-1] == f"installed commit={want} author=Colin"
    assert host.ran("build-rootfs.sh") == []
    assert host.installed == want


def test_a_given_reason_is_cut_to_plain_characters(host: Host) -> None:
    host.at(host.first)
    want = commit(host.origin, "ci-runner/supervisor/ci_supervisor.py", "x")

    assert host.update(want, "Colin: $(reboot); fix #3") == 0

    assert host.ran("install-host.sh")[0][-1] == "REASON=Colin: (reboot) fix #3"


def test_a_change_to_the_image_inputs_rebuilds_it_after_the_install(host: Host) -> None:
    host.at(host.first)
    want = commit(host.origin, "ci-runner/versions.env", "Update uv (#20)")

    assert host.update(want) == 0

    assert host.ran("install-host.sh") and host.ran("build-rootfs.sh")
    assert host.ran("build-rootfs.sh")[0][:5] == ["nice", "-n", "10", "ionice", "-c3"]
    assert host.built == want
    assert host.seen == [host.first, host.first]
    assert host.installed == want


def test_an_image_never_built_is_built_on_the_installed_commit(host: Host) -> None:
    host.at(host.first)
    (host.root / "var/lib/homelab-ci/images/commit").unlink()

    assert host.update(host.first) == 0

    assert host.ran("build-rootfs.sh") and host.ran("install-host.sh") == []
    assert host.built == host.first


def test_a_failed_install_keeps_the_old_commit_and_gives_up_after_two(host: Host) -> None:
    host.at(host.first)
    want = commit(host.origin, "ci-runner/rootfs/Dockerfile", "Image (#30)")
    host.codes["install-host.sh"] = 1
    now = time.time()

    assert host.update(want, now=now) == 1
    assert host.update(want, now=now) == 1
    assert host.update(want, now=now + 60) == 0

    assert len(host.ran("install-host.sh")) == 2
    assert host.ran("build-rootfs.sh") == []
    assert host.installed == host.first
    assert (host.root / "var/lib/homelab-ci/update-failed").read_text().split()[:2] == [want, "2"]


def test_a_failed_image_gives_up_after_two_and_jobs_keep_the_old_one(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    want = commit(host.origin, "ci-runner/rootfs/Dockerfile", "Image (#30)")
    host.at(want, built=host.first)
    host.codes["build-rootfs.sh"] = 1
    now = time.time()

    assert host.update(want, now=now) == 1
    assert host.update(want, now=now) == 1
    assert host.update(want, now=now + 60) == 0

    assert len(host.ran("build-rootfs.sh")) == 2
    assert host.built == host.first
    assert host.ran("install-host.sh") == []
    assert "jobs keep the old one" in capsys.readouterr().out


def test_a_change_that_gave_up_is_tried_again_an_hour_later(host: Host) -> None:
    host.at(host.first)
    want = commit(host.origin, "ci-runner/supervisor/ci_supervisor.py", "x")
    host.codes["install-host.sh"] = 1
    host.update(want, now=1000)
    host.update(want, now=1000)
    host.codes["install-host.sh"] = 0

    assert host.update(want, now=1000 + 3600) == 0

    assert len(host.ran("install-host.sh")) == 3
    assert host.installed == want


def test_the_commit_marker_is_written_last(host: Host) -> None:
    host.at(host.first)
    want = commit(host.origin, "ci-runner/build-rootfs.sh", "Build (#40)")

    assert host.update(want) == 0

    assert host.seen == [host.first, host.first]
    assert host.calls[-1][-1] == "start-load-test-if-due"
    assert host.installed == want


def test_a_load_test_that_will_not_start_waits_for_the_next_pass(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.at(host.first)
    host.codes["start-load-test-if-due"] = 1

    assert host.update(host.first) == 0

    assert "could not start the load test; next pass" in capsys.readouterr().out


def test_follow_takes_main(host: Host) -> None:
    host.at(host.first)
    want = commit(host.origin, "ci-runner/supervisor/ci_supervisor.py", "x")

    assert host.update("--follow") == 0

    assert host.installed == want


def test_follow_stays_on_a_pinned_commit(host: Host, capsys: pytest.CaptureFixture[str]) -> None:
    (host.root / "etc/homelab-ci/pinned").write_text(f"{host.first}\n")

    assert host.update("--follow") == 0

    assert host.calls == []
    assert f"rolled back by hand to {host.first};" in capsys.readouterr().out


def test_a_commit_not_on_main_is_refused(host: Host) -> None:
    host.at(host.first)
    git(host.src, "checkout", "-q", "-b", "side")
    other = commit(host.src, "ci-runner/supervisor/ci_supervisor.py", "side")

    assert host.update(other) == 2
    assert host.update("main") == 2
    assert host.installed == host.first


def test_an_older_commit_needs_rollback(host: Host) -> None:
    want = commit(host.origin, "ci-runner/supervisor/ci_supervisor.py", "x")
    host.at(want)

    assert host.update("--follow") == 0
    git(host.origin, "reset", "-q", "--hard", host.first)
    assert host.update("--follow") == 0
    assert host.update(host.first) == 2
    assert host.installed == want

    assert host.update(host.first, ROLLBACK="1") == 0
    assert host.installed == host.first
    assert (host.root / "etc/homelab-ci/pinned").read_text() == f"{host.first}\n"


def test_follow_gives_way_to_a_running_update(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    import fcntl

    with (host.root / "run/homelab-ci-update.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert host.update("--follow") == 0

    assert host.calls == []
    assert "another update is running" in capsys.readouterr().out


def test_no_argument_is_a_usage_error(host: Host) -> None:
    assert host.update() == 1


@pytest.mark.skipif(
    not Path("/usr/bin/python3").exists(), reason="the wrapper runs /usr/bin/python3"
)
def test_the_installed_copy_runs_alone(tmp_path: Path) -> None:
    # Installed alone in /usr/local/lib/homelab-ci: an import from the checkout would fail there.
    for name in ("update.py", "update.sh"):
        shutil.copy(CI_RUNNER / name, tmp_path / name)

    result = subprocess.run(
        ["sh", str(tmp_path / "update.sh")], cwd=tmp_path, capture_output=True, text=True
    )

    assert result.returncode == 1
    message = "update.sh: usage:" if os.geteuid() == 0 else "run with sudo"
    assert result.stderr.startswith(message), result.stderr


def test_the_units_and_the_updater_run_the_same_python() -> None:
    # install_host.py makes the link; a unit or SUPERVISOR naming another python3 would run the
    # repo's code on the host's 3.12, or fail where the link is missing.
    spec = importlib.util.spec_from_file_location(
        "install_host_link", CI_RUNNER / "install_host.py"
    )
    assert spec is not None and spec.loader is not None
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    commands = [
        line.split("=", 1)[1].split()
        for unit in units()
        for line in unit.read_text().splitlines()
        if line.startswith("ExecStart=") and "/usr/local/lib/homelab-ci/" in line and ".py" in line
    ]
    assert len(commands) == 5
    assert {words[0] for words in commands} == {installer.PYTHON_LINK}
    assert up.SUPERVISOR[0] == installer.PYTHON_LINK
    # -I would drop its folder, where telemetry.py is installed, from the path.
    assert "-Es" in up.SUPERVISOR

    # The wrappers too; install-host.sh and update.sh repair the host, so they stay on the system's.
    wrappers = [
        line.split()[1]
        for script in CI_RUNNER.glob("**/*.sh")
        if script.name not in ("install-host.sh", "update.sh")
        for line in script.read_text().splitlines()
        if line.startswith("exec ") and ".py" in line
    ]
    assert len(wrappers) == 8
    assert set(wrappers) == {installer.PYTHON_LINK}
