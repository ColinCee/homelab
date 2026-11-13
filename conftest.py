import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from functools import cache
from pathlib import Path
from time import monotonic
from typing import Any, NamedTuple

import pytest
import yaml

# CI's time limits, which fail the run in CI: one test process's run (checks/test_shards.py runs
# one per core) at about twice today's, and one test's own body (a sleep, a timeout, a container
# started per test). Shared fixtures count in the run. Raise one only in a pull request saying why.
# Both are own_seconds: waiting counts, but not the time the host gave the VM's CPUs to other VMs
# or services (13 Nov: two VMs at once took every shard from 15 s to 30 s). So while other VMs run,
# a test that only computes gets up to about twice the limit before it fails.
RUN_LIMIT = 30.0
TEST_LIMIT = 5.0

# Each test's wall-clock first start and its seconds over setup, call and teardown.
TIMES: dict[str, list[float]] = {}
CALLS: dict[str, float] = {}


class Mark(NamedTuple):
    """A moment: seconds on the clock and of this process's CPU (with its finished children's),
    and the whole machine's seconds stolen by the host and of CPU it got (all zero off Linux)."""

    wall: float
    cpu: float
    stolen: float
    busy: float


def mark(stat: Path = Path("/proc/stat")) -> Mark:
    # The clock bound at import, which a test faking time.monotonic does not change.
    wall, used = monotonic(), os.times()
    try:
        fields = stat.read_text(encoding="ascii").split("\n", 1)[0].split()
    except OSError:
        fields = []
    # The CPUs' total, in USER_HZ (100 a second): user nice system idle iowait irq softirq steal.
    ticks = [int(f) / 100 for f in fields[1:9]] if fields[:1] == ["cpu"] else [0.0] * 8
    busy = ticks[0] + ticks[1] + ticks[2] + ticks[5] + ticks[6]
    cpu = used.user + used.system + used.children_user + used.children_system
    return Mark(wall, cpu, ticks[7], busy)


def own_seconds(start: Mark, end: Mark) -> float:
    """Wall seconds less what the host took from this process: the machine's stolen share of the
    seconds it was on a CPU, so a test that only waits gets nothing off. A VM's process CPU time
    includes steal (13 Nov: shards 63.5 s, the VM's own 35.4 s), and children's CPU time can run
    side by side, so it counts at most the wall time."""
    wall, cpu = end.wall - start.wall, end.cpu - start.cpu
    wanted = end.busy - start.busy + end.stolen - start.stolen
    share = (end.stolen - start.stolen) / wanted if wanted > 0 else 0.0
    return wall - share * max(0.0, min(cpu, wall))


STARTED = [mark()]


def pytest_sessionstart(session: pytest.Session) -> None:
    STARTED[0] = mark()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> object:
    start = mark()
    try:
        return (yield)
    finally:
        CALLS[item.nodeid] = own_seconds(start, mark())


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    start, took = TIMES.get(report.nodeid, [report.start, 0.0])
    TIMES[report.nodeid] = [min(start, report.start), took + report.stop - report.start]


def over_limits(run_seconds: float, calls: dict[str, float]) -> list[str]:
    lines = [
        f"{test} took {took:.1f} s, over TEST_LIMIT ({TEST_LIMIT:.0f} s)"
        for test, took in calls.items()
        if took > TEST_LIMIT
    ]
    if run_seconds > RUN_LIMIT:
        lines.append(f"The run took {run_seconds:.0f} s, over RUN_LIMIT ({RUN_LIMIT:.0f} s)")
    return lines


def pytest_sessionfinish(session: pytest.Session) -> None:
    over = over_limits(own_seconds(STARTED[0], mark()), CALLS)
    for line in over:
        print()
        print(f"{line}. Make it faster, or raise the limit here in a pull request saying why.")
    if over and os.environ.get("CI") and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    # The slowest tests for ci-runner/run-times/ci_times.py, which ci.yaml posts as an annotation.
    if out := os.environ.get("TEST_TIMES"):
        write_slowest(Path(out))


def write_slowest(out: Path) -> None:
    """[[test, start, seconds], ...], slowest first, under the 4096 characters GitHub keeps of an
    annotation (with room for escaping)."""
    rows: list[list[Any]] = []
    for test, (start, took) in sorted(TIMES.items(), key=lambda item: -item[1][1]):
        row = [test[-150:], round(start, 2), round(took, 2)]
        if len(json.dumps([*rows, row], separators=(",", ":"))) > 3500:
            break
        rows.append(row)
    out.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")


ROOT = Path(__file__).parent
STACKS = ROOT / "stacks"


@cache
def tracked(*endings: str) -> tuple[Path, ...]:
    """Every file in git ending so, wherever it is: a check that names folders misses a new one."""
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    return tuple(ROOT / p for p in listed if not endings or p.endswith(endings))


def is_test(path: Path) -> bool:
    return path.name.endswith("_test.py") or path.name == "conftest.py"


def programs() -> list[Path]:
    """Every Python file but the tests."""
    return [p for p in tracked(".py") if not is_test(p)]


def units() -> tuple[Path, ...]:
    return tracked(".service")


class ComposeLoader(yaml.SafeLoader):
    """Reads Compose's own tags (`!reset`, `!override`) as plain values."""


def _plain(loader: yaml.SafeLoader, _tag: str, node: yaml.Node) -> object:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


ComposeLoader.add_multi_constructor("!", _plain)


def load_yaml(path: Path) -> object:
    return yaml.load(path.read_text(encoding="utf-8"), ComposeLoader)


@cache
def stacks() -> dict[Path, dict[str, Any]]:
    """Every stack's compose.yaml, by its folder: the deploy's own list (docs/architecture.md,
    "Deploys")."""
    found = {}
    for compose in sorted(STACKS.glob("*/compose.yaml")):
        spec = load_yaml(compose)
        assert isinstance(spec, dict), compose
        found[compose.parent] = spec
    return found


def dockerfile(stack: Path, service: dict[str, Any]) -> Path:
    """A built service's Dockerfile, which Compose names Dockerfile unless told otherwise."""
    build = service["build"]
    return stack / str(build["context"]) / str(build.get("dockerfile", "Dockerfile"))


def services() -> list[tuple[Path, str, dict[str, Any]]]:
    """Every Compose service: its stack's folder, its name and its spec."""
    return [
        (stack, name, s) for stack, spec in stacks().items() for name, s in spec["services"].items()
    ]


# A commit needs an author, and a test must not sign with the caller's key or depend on their
# default branch.
GIT = [
    *("git", "-c", "user.name=Colin", "-c", "user.email=c@example.com"),
    *("-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main"),
]


def git(repo: Path, *args: str) -> str:
    """Every test's git: in `repo`, as a throwaway author, its output stripped."""
    done = subprocess.run(
        [*GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def promtool(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([tool("promtool"), *args], capture_output=True, text=True, timeout=300)


def tool(name: str) -> str:
    """The path of a mise.toml tool such as promtool: skipped without it, but failed in CI."""
    path = shutil.which(name)
    if path:
        return path
    if os.environ.get("CI"):
        pytest.fail(f"{name} is not on PATH; mise.toml installs it")
    pytest.skip(f"needs {name} (mise install)")


def linux_only(why: str) -> None:
    """Skip the rest of the test, or the module, off Linux (Colin's Windows PC) when it needs
    what only Linux has, such as file modes, flock or uids; CI runs Linux, so there it fails."""
    if sys.platform == "linux":
        return
    if os.environ.get("CI"):
        pytest.fail(f"needs Linux ({why}), which CI runs")
    pytest.skip(f"needs Linux: {why}", allow_module_level=True)


class NoGitHub:
    """The CI supervisor's GitHub (ci_supervisor.py), for code that must not ask it. A fake
    subclasses it and gives `request` GitHub's answers."""

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> object:
        raise AssertionError(f"GitHub was asked: {method} {path}")

    def forget(self, drop: Callable[[str], bool]) -> None:
        pass

    def clone(self) -> NoGitHub:
        return self


@pytest.fixture(autouse=True)
def sent_traces(monkeypatch: pytest.MonkeyPatch) -> list[Mapping[str, Any]]:
    """What a traced run would have sent Alloy: no test reaches it, or joins its caller's trace."""
    import telemetry

    sent: list[Mapping[str, Any]] = []
    monkeypatch.setattr(telemetry, "send_traces", lambda body, **_: sent.append(body))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    return sent


@pytest.fixture(autouse=True)
def _no_outer_git(monkeypatch: pytest.MonkeyPatch) -> None:
    # An inherited GIT_DIR or GIT_WORK_TREE sends a test's `git init` into the caller's repository.
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
