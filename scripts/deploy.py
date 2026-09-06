#!/usr/bin/env python3
"""Validate and deploy self-contained Compose stacks."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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

    for plan in plans:
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
                env=_compose_environment(),
                check=True,
            )
            install_systemd_units(plan.timers, unit_dir=unit_dir, runner=runner)
        except (OSError, subprocess.CalledProcessError) as exc:
            reason = _failure_reason(exc)
            failed[plan.name] = reason
            print(f"Deployment failed for {plan.name}: {reason}", file=sys.stderr)
            continue

        completed.append(plan.name)
        print(f"Deployed: {plan.name}")

    report = DeploymentReport(tuple(completed), failed)
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
    parser.add_argument("stacks", nargs="*", help="stack names (default: all discovered stacks)")
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]

    try:
        plans = discover_stack_plans(repo_root, tuple(args.stacks) or None)
        report = deploy(repo_root, plans, readiness_timeout=args.readiness_timeout)
    except (DeploymentInputError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
