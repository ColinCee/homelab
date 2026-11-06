#!/usr/bin/env python3
"""Validate and deploy self-contained Compose stacks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

CommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]


class DeploymentInputError(ValueError):
    """Raised when deployment inputs are invalid before any mutation."""


@dataclass(frozen=True)
class StackPlan:
    """Validated input paths for one stack."""

    name: str
    compose_file: Path
    env_file: Path | None
    timers: tuple[Path, ...]


@dataclass(frozen=True)
class DeploymentReport:
    """Outcome of applying all plans after validation."""

    completed: tuple[str, ...]
    failed: Mapping[str, str]
    # When each completed stack's `up --wait` returned, as ISO UTC times.
    ready_at: Mapping[str, str] = field(default_factory=dict)

    @property
    def partial(self) -> bool:
        return bool(self.completed) and bool(self.failed)


def discover_stack_plans(
    repo_root: Path,
    selected_names: Sequence[str] | None = None,
) -> tuple[StackPlan, ...]:
    """Discover all or explicitly selected Compose stacks and their unit pairs."""
    stacks_dir = repo_root / "stacks"
    if not stacks_dir.is_dir():
        raise DeploymentInputError(f"Missing stacks directory: {stacks_dir}")

    if selected_names:
        names = tuple(selected_names)
    else:
        names = tuple(
            sorted(
                compose.parent.name
                for compose in stacks_dir.glob("*/compose.yaml")
                if compose.is_file()
            )
        )
    if not names:
        raise DeploymentInputError("No stacks selected and no stacks/*/compose.yaml files found")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise DeploymentInputError(f"Duplicate stack selection(s): {', '.join(duplicates)}")

    errors: list[str] = []
    plans: list[StackPlan] = []
    for name in names:
        try:
            _validate_stack_name(name)
            stack_dir = stacks_dir / name
            compose_file = stack_dir / "compose.yaml"
            if not compose_file.is_file():
                raise DeploymentInputError(f"{name}: missing {compose_file}")

            example_file = stack_dir / ".env.example"
            env_file = stack_dir / ".env" if example_file.is_file() else None
            if env_file is not None and not env_file.is_file():
                raise DeploymentInputError(
                    f"{name}: missing {env_file}; render it with scripts/generate-env.sh"
                )

            plans.append(
                StackPlan(
                    name=name,
                    compose_file=compose_file,
                    env_file=env_file,
                    timers=discover_timers(stack_dir),
                )
            )
        except DeploymentInputError as exc:
            errors.append(str(exc))

    if errors:
        raise DeploymentInputError("\n".join(errors))
    return tuple(plans)


# Files every stack's deploy depends on: a change to any of them deploys all stacks.
SHARED_PATHS = ("scripts/", ".github/workflows/deploy.yaml")


def changed_stacks(repo_root: Path, since: str | None) -> tuple[str, ...] | None:
    """Name the stacks with files changed between ``since`` and HEAD.

    None means deploy every stack: no earlier deploy is known, git does not
    know the commit, or shared deploy files changed. Deleted stacks are left
    out, as deploys never remove a stack.
    """
    paths = _changed_paths(repo_root, since)
    if paths is None or any(path.startswith(SHARED_PATHS) for path in paths):
        return None
    return _stacks_in(repo_root, paths)


def _changed_paths(repo_root: Path, since: str | None) -> list[str] | None:
    if not since:
        return None
    listed = subprocess.run(
        ["git", "diff", "--name-only", since, "HEAD", "--"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return listed.stdout.splitlines() if listed.returncode == 0 else None


def _stacks_in(repo_root: Path, paths: Sequence[str]) -> tuple[str, ...]:
    names = {
        path.split("/")[1] for path in paths if path.startswith("stacks/") and path.count("/") >= 2
    }
    return tuple(
        sorted(name for name in names if (repo_root / "stacks" / name / "compose.yaml").is_file())
    )


def changed_first(
    repo_root: Path,
    plans: Sequence[StackPlan],
    since: str | None,
    *,
    runner: CommandRunner = subprocess.run,
) -> tuple[StackPlan, ...]:
    """Move the stacks changed since ``since`` to the front, so the push's change goes live first.

    A changed stack still comes after any stack that creates a network or volume it uses as
    external, so a fresh host can create it first. The old order stays when git does not know
    ``since`` or Compose cannot describe a stack.
    """
    paths = _changed_paths(repo_root, since)
    changed = set(_stacks_in(repo_root, paths)) if paths is not None else set()
    if not changed & {plan.name for plan in plans}:
        return tuple(plans)
    try:
        resources = {plan.name: _shared_resources(repo_root, plan, runner) for plan in plans}
    except (OSError, ValueError, subprocess.CalledProcessError):
        return tuple(plans)

    ordered: list[str] = []

    def place(name: str, waiting: frozenset[str]) -> None:
        if name in ordered or name in waiting:
            return
        uses = resources[name][1]
        for other in resources:
            if other != name and uses & resources[other][0]:
                place(other, waiting | {name})
        ordered.append(name)

    for plan in plans:
        if plan.name in changed:
            place(plan.name, frozenset())
    by_name = {plan.name: plan for plan in plans}
    return tuple(by_name[name] for name in ordered) + tuple(
        plan for plan in plans if plan.name not in ordered
    )


def _shared_resources(
    repo_root: Path, plan: StackPlan, runner: CommandRunner
) -> tuple[set[str], set[str]]:
    """The network and volume names a stack creates, and the external ones it needs."""
    described = runner(
        compose_command(plan, "config", "--format", "json"),
        cwd=repo_root,
        env=_compose_environment(),
        check=True,
        capture_output=True,
    )
    config = json.loads(described.stdout)
    creates: set[str] = set()
    uses: set[str] = set()
    for kind in ("networks", "volumes"):
        for key, resource in (config.get(kind) or {}).items():
            resource = resource or {}
            name = resource.get("name") or key
            (uses if resource.get("external") else creates).add(name)
    return creates, uses


def head_commit(repo_root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout.strip()


def discover_timers(stack_dir: Path) -> tuple[Path, ...]:
    """Find stack-prefixed service/timer pairs under a stack's systemd directory."""
    systemd_dir = stack_dir / "systemd"
    if not systemd_dir.is_dir():
        return ()

    units = sorted(
        path
        for path in systemd_dir.iterdir()
        if path.is_file() and path.suffix in {".service", ".timer"}
    )
    errors: list[str] = []
    required_prefix = f"{stack_dir.name}-"
    for unit in units:
        if not unit.stem.startswith(required_prefix):
            errors.append(f"{unit}: unit name must start with {required_prefix!r}")
        counterpart = ".service" if unit.suffix == ".timer" else ".timer"
        if not unit.with_suffix(counterpart).is_file():
            errors.append(f"{unit}: needs both .service and .timer files")

    if errors:
        raise DeploymentInputError("\n".join(errors))
    return tuple(unit for unit in units if unit.suffix == ".timer")


def validate_plans(
    repo_root: Path,
    plans: Sequence[StackPlan],
    *,
    runner: CommandRunner = subprocess.run,
) -> None:
    """Validate every Compose model before building or starting any stack."""
    errors: list[str] = []
    for plan in plans:
        command = compose_command(plan, "config", "--quiet", all_profiles=True)
        try:
            runner(
                command,
                cwd=repo_root,
                env=_compose_environment(),
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            errors.append(f"{plan.name}: Compose validation failed: {_failure_reason(exc)}")

    if errors:
        raise DeploymentInputError("\n".join(errors))


def deploy(
    repo_root: Path,
    plans: Sequence[StackPlan],
    *,
    readiness_timeout: int = 120,
    unit_dir: Path | None = None,
    runner: CommandRunner = subprocess.run,
) -> DeploymentReport:
    """Build, start, wait for readiness, and install each selected stack."""
    if readiness_timeout < 1:
        raise DeploymentInputError("readiness timeout must be a positive integer")

    validate_plans(repo_root, plans, runner=runner)
    completed: list[str] = []
    failed: dict[str, str] = {}
    ready_at: dict[str, str] = {}

    for plan in plans:
        environment = _compose_environment()
        environment["STACK_CONFIG_HASH"] = stack_config_hash(repo_root, plan.compose_file.parent)
        try:
            runner(
                compose_command(plan, "build", all_profiles=True),
                cwd=repo_root,
                env=_compose_environment(),
                check=True,
            )
            runner(
                compose_command(
                    plan,
                    "up",
                    "-d",
                    "--remove-orphans",
                    "--wait",
                    "--wait-timeout",
                    str(readiness_timeout),
                ),
                cwd=repo_root,
                env=environment,
                check=True,
            )
            ready = datetime.now(UTC).isoformat()
            install_systemd_units(plan.timers, unit_dir=unit_dir, runner=runner)
        except (OSError, subprocess.CalledProcessError) as exc:
            reason = _failure_reason(exc)
            failed[plan.name] = reason
            print(f"Deployment failed for {plan.name}: {reason}", file=sys.stderr)
            continue

        completed.append(plan.name)
        ready_at[plan.name] = ready
        print(f"Deployed: {plan.name}", flush=True)

    report = DeploymentReport(tuple(completed), failed, ready_at)
    if report.partial:
        print(
            "Partial deployment: completed "
            + ", ".join(report.completed)
            + "; failed "
            + ", ".join(report.failed),
            file=sys.stderr,
        )
    elif report.failed:
        print("Deployment failed for all selected stacks", file=sys.stderr)
    return report


def stack_config_hash(repo_root: Path, stack_dir: Path) -> str:
    """Hash a stack's tracked config files.

    Compose does not notice edits to bind-mounted files, so services that
    should restart on config changes put this in a label. Only git-tracked
    files count, which keeps runtime data dirs (config/, data/) out. Outside
    a git checkout the hash is empty, so labels never change.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", str(stack_dir.relative_to(repo_root))],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if listed.returncode != 0:
        return ""
    digest = hashlib.sha256()
    for name in sorted(filter(None, listed.stdout.split(b"\0"))):
        path = repo_root / name.decode()
        if path.name == "compose.yaml" or not path.is_file():
            continue
        digest.update(name + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def compose_command(
    plan: StackPlan,
    *arguments: str,
    all_profiles: bool = False,
) -> list[str]:
    command = ["docker", "compose"]
    if plan.env_file is not None:
        command.extend(["--env-file", str(plan.env_file)])
    command.extend(["-f", str(plan.compose_file)])
    if all_profiles:
        command.extend(["--profile", "*"])
    command.extend(arguments)
    return command


def install_systemd_units(
    timers: Sequence[Path],
    *,
    unit_dir: Path | None = None,
    runner: CommandRunner = subprocess.run,
) -> None:
    """Atomically install pairs and enable their timers through the user manager."""
    if not timers:
        return

    target_dir = unit_dir or _default_unit_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    for timer in timers:
        for unit in (timer.with_suffix(".service"), timer):
            _atomic_copy(unit, target_dir / unit.name)

    environment = _systemctl_environment()
    runner(["systemctl", "--user", "daemon-reload"], env=environment, check=True)
    for timer in timers:
        runner(
            ["systemctl", "--user", "enable", "--now", timer.name],
            env=environment,
            check=True,
        )


def _validate_stack_name(name: str) -> None:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise DeploymentInputError(f"Invalid stack name: {name!r}")


def _default_unit_dir() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "systemd" / "user"


def _systemctl_environment() -> dict[str, str]:
    allowed = {
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "USER",
        "XDG_CONFIG_HOME",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    runtime_dir = environment.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    environment["XDG_RUNTIME_DIR"] = runtime_dir
    environment.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
    return environment


def _compose_environment() -> dict[str, str]:
    allowed = {
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_CERT_PATH",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "NO_COLOR",
        "PATH",
        "TERM",
        "USER",
        "XDG_CONFIG_HOME",
        "XDG_RUNTIME_DIR",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment["COMPOSE_PROFILES"] = ""
    return environment


def _atomic_copy(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(source, temporary)
        shutil.copymode(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _failure_reason(error: BaseException) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        return f"command exited with {error.returncode}"
    return str(error)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--readiness-timeout",
        type=_positive_int,
        default=os.environ.get("DEPLOY_READINESS_TIMEOUT_SECONDS", "120"),
        help="Compose readiness wait in seconds (default: 120)",
    )
    parser.add_argument(
        "--last-deployed",
        type=Path,
        help="file holding the last fully deployed commit: deploy only stacks changed since"
        " it (all when unknown or shared deploy files changed), and record HEAD there"
        " once every selected stack deploys",
    )
    parser.add_argument(
        "--first-changed-since",
        metavar="COMMIT",
        help="deploy the stacks changed since COMMIT first, after any stack creating a network"
        " or volume they use; the usual name order when git does not know COMMIT",
    )
    parser.add_argument("stacks", nargs="*", help="stack names (default: all discovered stacks)")
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    return run(
        repo_root,
        tuple(args.stacks),
        readiness_timeout=args.readiness_timeout,
        last_deployed=args.last_deployed,
        first_changed_since=args.first_changed_since,
    )


def run(
    repo_root: Path,
    stacks: Sequence[str],
    *,
    readiness_timeout: int = 120,
    last_deployed: Path | None = None,
    first_changed_since: str | None = None,
    runner: CommandRunner = subprocess.run,
) -> int:
    """Deploy the given, changed or all stacks; exit code 0 ok, 1 failed, 2 bad input."""
    selected: tuple[str, ...] | None = tuple(stacks) or None
    head = ""
    if last_deployed is not None and selected is None:
        head = head_commit(repo_root)
        since = last_deployed.read_text().strip() if last_deployed.is_file() else None
        selected = changed_stacks(repo_root, since)
        print(
            "Deploying " + ("all stacks" if selected is None else ", ".join(selected) or "nothing")
        )

    try:
        plans = discover_stack_plans(repo_root, selected) if selected != () else ()
        if first_changed_since and plans:
            plans = changed_first(repo_root, plans, first_changed_since, runner=runner)
            print("In order: " + ", ".join(plan.name for plan in plans), flush=True)
        report = (
            deploy(repo_root, plans, readiness_timeout=readiness_timeout, runner=runner)
            if plans
            else DeploymentReport((), {})
        )
    except (DeploymentInputError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as file:
            file.write(f"deployed={json.dumps(list(report.completed))}\n")
            file.write(f"ready_at={json.dumps(dict(report.ready_at))}\n")
    if report.failed:
        return 1
    if head and last_deployed is not None:
        last_deployed.parent.mkdir(parents=True, exist_ok=True)
        temporary = last_deployed.with_name(f".{last_deployed.name}.tmp")
        temporary.write_text(head + "\n")
        os.replace(temporary, last_deployed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
