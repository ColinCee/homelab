#!/usr/bin/env python3
"""Validate and deploy self-contained Compose stacks."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

# The shared module, from the checkout it deploys (stacks/telemetry).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stacks/telemetry"))
import telemetry

CommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]

# Alloy's textfile folder (prometheus/rules.yml reads what the deploy writes there).
METRICS_DIR = Path("/var/lib/homelab-metrics")
# Restart policies of services that should always be running; any other is a one-shot.
LONG_RUNNING = {"always", "unless-stopped"}


class DeploymentInputError(ValueError):
    """Raised when deployment inputs are invalid before any mutation."""


@dataclass(frozen=True)
class StackPlan:
    """Validated input paths for one stack."""

    name: str
    compose_file: Path
    env_file: Path | None
    triggers: tuple[Path, ...]


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

            # The stack's own settings, tracked; secrets are files (secret_files).
            settings = stack_dir / "settings.env"
            env_file = settings if settings.is_file() else None
            for secret in secret_files(stack_dir):
                if not secret.is_file():
                    raise DeploymentInputError(
                        f"{name}: missing {secret}; copy it from Bitwarden (docs/recovery.md)"
                    )

            plans.append(
                StackPlan(
                    name=name,
                    compose_file=compose_file,
                    env_file=env_file,
                    triggers=discover_triggers(stack_dir),
                )
            )
        except DeploymentInputError as exc:
            errors.append(str(exc))

    if errors:
        raise DeploymentInputError("\n".join(errors))
    return tuple(plans)


# Files every stack's deploy depends on: a change to any of them deploys all stacks. Only the
# deploy's own scripts, so a change to another (the CI supervisor, say) deploys nothing.
SHARED_PATHS = (
    "scripts/deploy.sh",
    "scripts/deploy.py",
)


def changed_stacks(repo_root: Path, since: str | None) -> tuple[str, ...] | None:
    """Name the stacks with files changed between ``since`` and HEAD.

    None means deploy every stack: no earlier deploy is known, git does not
    know the commit, or shared files changed: the deploy's own, or a folder under stacks/ that is
    no stack (stacks/telemetry), which any stack may mount. Deleted stacks are left out, as deploys
    never remove a stack.
    """
    paths = _changed_paths(repo_root, since)
    if paths is None or any(path.startswith(SHARED_PATHS) for path in paths):
        return None
    if any(_shared_under_stacks(repo_root, path) for path in paths):
        return None
    return _stacks_in(repo_root, paths)


def _shared_under_stacks(repo_root: Path, path: str) -> bool:
    parts = path.split("/")
    # A deleted shared folder counts too: its mounts went with it.
    return (
        len(parts) >= 3
        and parts[0] == "stacks"
        and not (repo_root / "stacks" / parts[1] / "compose.yaml").is_file()
    )


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
    external, so a fresh host can create it first. When git does not know ``since`` (a fresh
    host), every stack counts as changed. The old order stays when Compose cannot describe one.
    """
    paths = _changed_paths(repo_root, since)
    names = {plan.name for plan in plans}
    changed = set(_stacks_in(repo_root, paths)) if paths is not None else names
    if not changed & names:
        return tuple(plans)
    try:
        resources = {plan.name: _shared_resources(repo_root, plan, runner) for plan in plans}
    except (OSError, TypeError, ValueError, subprocess.CalledProcessError):
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


TRIGGER_SUFFIXES = (".timer", ".path")


def discover_triggers(stack_dir: Path) -> tuple[Path, ...]:
    """Find a stack's stack-prefixed services and the .timer and .path units that start them;
    each service needs at least one, and each trigger its service."""
    systemd_dir = stack_dir / "systemd"
    if not systemd_dir.is_dir():
        return ()

    units = sorted(
        path
        for path in systemd_dir.iterdir()
        if path.is_file() and path.suffix in {".service", *TRIGGER_SUFFIXES}
    )
    errors: list[str] = []
    required_prefix = f"{stack_dir.name}-"
    for unit in units:
        if not unit.stem.startswith(required_prefix):
            errors.append(f"{unit}: unit name must start with {required_prefix!r}")
        if unit.suffix == ".service":
            if not any(unit.with_suffix(suffix).is_file() for suffix in TRIGGER_SUFFIXES):
                errors.append(f"{unit}: needs a .timer or .path file")
        elif not unit.with_suffix(".service").is_file():
            errors.append(f"{unit}: needs a .service file")

    if errors:
        raise DeploymentInputError("\n".join(errors))
    return tuple(unit for unit in units if unit.suffix in TRIGGER_SUFFIXES)


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
    metrics_dir: Path | None = None,
    stamp_dir: Path | None = None,
    runner: CommandRunner = subprocess.run,
) -> DeploymentReport:
    """Build, start, wait for readiness, and install each selected stack, then record what it
    runs in ``metrics_dir`` when given."""
    if readiness_timeout < 1:
        raise DeploymentInputError("readiness timeout must be a positive integer")

    validate_plans(repo_root, plans, runner=runner)
    completed: list[str] = []
    failed: dict[str, str] = {}
    ready_at: dict[str, str] = {}

    for plan in plans:
        environment = _compose_environment()
        environment |= config_hashes(repo_root, plan.compose_file.parent)
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
            install_systemd_units(plan.triggers, unit_dir=unit_dir, runner=runner)
            if metrics_dir is not None:
                record_running(repo_root, plan, metrics_dir, stamp_dir=stamp_dir, runner=runner)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
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


def record_running(
    repo_root: Path,
    plan: StackPlan,
    metrics_dir: Path,
    *,
    stamp_dir: Path | None = None,
    runner: CommandRunner = subprocess.run,
) -> None:
    """Write deploy-<stack>.prom: the services that should be running, for "A service stopped",
    and when systemd last started each timer, for "Monthly upkeep didn't run".

    A timer's time is its Persistent= stamp, which systemd creates when the timer is first
    enabled and touches each time it fires, so a timer that never fires still has one.
    """
    described = runner(
        compose_command(plan, "config", "--format", "json"),
        cwd=repo_root,
        env=_compose_environment(),
        check=True,
        capture_output=True,
    )
    config = json.loads(described.stdout)
    project = config.get("name") or plan.name
    lines = [
        "# HELP homelab_container_expected A service the last deploy started to keep running.",
        "# TYPE homelab_container_expected gauge",
    ]
    lines += [
        f'homelab_container_expected{{project="{project}",service="{name}"}} 1'
        for name, service in sorted((config.get("services") or {}).items())
        if (service or {}).get("restart") in LONG_RUNNING
    ]
    metric = "homelab_timer_last_trigger_timestamp_seconds"
    lines += [
        f"# HELP {metric} When systemd last started the timer's service, or first enabled it.",
        f"# TYPE {metric} gauge",
    ]
    stamps = stamp_dir or _default_stamp_dir()
    for timer in (unit for unit in plan.triggers if unit.suffix == ".timer"):
        stamp = stamps / f"stamp-{timer.name}"
        if stamp.is_file():
            lines.append(f'{metric}{{timer="{timer.stem}"}} {stamp.stat().st_mtime:.0f}')
    out = metrics_dir / f"deploy-{plan.name}.prom"
    telemetry.replace_file(out, "\n".join(lines) + "\n")


def hash_variable(service: str) -> str:
    """The variable a service's `homelab.config-hash` label reads (config_hashes)."""
    return "CONFIG_HASH_" + re.sub(r"\W", "_", service).upper()


def config_hashes(repo_root: Path, stack_dir: Path) -> dict[str, str]:
    """Each service's hash of the tracked files it bind-mounts from the stacks folder, and when each
    of its secret files last changed, by hash_variable.

    Compose does not notice edits to bind-mounted files, so a service that should restart on them
    puts its hash in a label. Per service, so a change restarts only the services that read it.
    Only git-tracked files count, which keeps runtime data (config/, data/) out. A secret counts by
    its file's time alone, since anything that may inspect containers reads the label. Outside a
    git checkout every hash is empty, so labels never change.
    """
    compose = yaml.safe_load((stack_dir / "compose.yaml").read_text(encoding="utf-8")) or {}
    services = compose.get("services") or {}
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", str(stack_dir.parent.relative_to(repo_root))],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if listed.returncode != 0:
        return {hash_variable(name): "" for name in services}
    # Paths as git lists them, not resolved: a tracked symlink stays inside the repo.
    tracked = sorted(Path(name.decode()) for name in filter(None, listed.stdout.split(b"\0")))
    stack = stack_dir.relative_to(repo_root)
    secret_paths = {
        name: Path(os.path.normpath(stack_dir / spec["file"]))
        for name, spec in (compose.get("secrets") or {}).items()
        if isinstance(spec, dict) and "file" in spec
    }
    hashes: dict[str, str] = {}
    for name, spec in services.items():
        sources = [Path(os.path.normpath(stack / source)) for source in _bind_sources(spec)]
        digest = hashlib.sha256()
        for path in tracked:
            if not any(path == s or s in path.parents for s in sources):
                continue
            if (repo_root / path).is_file():
                digest.update(path.as_posix().encode() + b"\0")
                digest.update((repo_root / path).read_bytes() + b"\0")
        for secret in spec.get("secrets") or []:
            source = secret if isinstance(secret, str) else secret.get("source", "")
            path = secret_paths.get(source)
            if path and path.is_file():
                digest.update(f"{path.name}\0{path.stat().st_mtime_ns}\0".encode())
        hashes[hash_variable(name)] = digest.hexdigest()[:16]
    return hashes


def _bind_sources(spec: Mapping[str, Any]) -> list[str]:
    """A service's bind-mounted host paths relative to its stack folder (./name, ../telemetry)."""
    sources: list[str] = []
    for volume in spec.get("volumes") or []:
        source = volume.split(":")[0] if isinstance(volume, str) else str(volume.get("source", ""))
        if source.startswith(("./", "../")):
            sources.append(source)
    return sources


def stacks_with_secrets_since(repo_root: Path, since_ns: int) -> tuple[str, ...]:
    """The stacks reading a secret rendered after ``since_ns``, as a new secret changes no tracked
    file."""

    def renewed(secret: Path) -> bool:
        return secret.is_file() and secret.stat().st_mtime_ns > since_ns

    return tuple(
        sorted(
            stack_dir.name
            for stack_dir in (repo_root / "stacks").iterdir()
            if (stack_dir / "compose.yaml").is_file() and any(map(renewed, secret_files(stack_dir)))
        )
    )


def secret_files(stack_dir: Path) -> tuple[Path, ...]:
    """The files a stack's compose.yaml mounts as secrets (docs/recovery.md)."""
    compose = yaml.safe_load((stack_dir / "compose.yaml").read_text(encoding="utf-8")) or {}
    secrets = compose.get("secrets") or {}
    return tuple(
        Path(os.path.normpath(stack_dir / spec["file"]))
        for spec in secrets.values()
        if isinstance(spec, dict) and "file" in spec
    )


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
    triggers: Sequence[Path],
    *,
    unit_dir: Path | None = None,
    runner: CommandRunner = subprocess.run,
) -> None:
    """Atomically install each trigger and its service, and enable the triggers through the user
    manager."""
    if not triggers:
        return

    target_dir = unit_dir or _default_unit_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    for trigger in triggers:
        for unit in (trigger.with_suffix(".service"), trigger):
            telemetry.replace_file(target_dir / unit.name, unit.read_bytes())

    environment = _systemctl_environment()
    runner(["systemctl", "--user", "daemon-reload"], env=environment, check=True)
    for trigger in triggers:
        runner(
            ["systemctl", "--user", "enable", "--now", trigger.name],
            env=environment,
            check=True,
        )


def _validate_stack_name(name: str) -> None:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise DeploymentInputError(f"Invalid stack name: {name!r}")


def _default_unit_dir() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "systemd" / "user"


def _default_stamp_dir() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / "systemd" / "timers"


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
    # No COMPOSE_PROFILES: a stack's settings.env turns its profiles on, which this would override.
    return {key: value for key, value in os.environ.items() if key in allowed}


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
        default=120,
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
    metrics_dir = METRICS_DIR if METRICS_DIR.is_dir() else None
    if metrics_dir is None:
        print(f"No {METRICS_DIR}: the alerts will not know what runs", file=sys.stderr)
    with one_deploy_at_a_time(args.last_deployed):
        return run(
            repo_root,
            tuple(args.stacks),
            readiness_timeout=args.readiness_timeout,
            last_deployed=args.last_deployed,
            first_changed_since=args.first_changed_since,
            metrics_dir=metrics_dir,
        )


@contextlib.contextmanager
def one_deploy_at_a_time(last_deployed: Path | None) -> Generator[None]:
    """Wait for any other deploy recording to the same file: a ring's and one run by hand."""
    if last_deployed is None:
        yield
        return
    import fcntl  # Linux only, and only here, so the tests import this module on any system.

    last_deployed.parent.mkdir(parents=True, exist_ok=True)
    with open(last_deployed.with_name("deploy.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Waiting for another deploy to finish", flush=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def run(
    repo_root: Path,
    stacks: Sequence[str],
    *,
    readiness_timeout: int = 120,
    last_deployed: Path | None = None,
    first_changed_since: str | None = None,
    metrics_dir: Path | None = None,
    runner: CommandRunner = subprocess.run,
) -> int:
    """Deploy the given, changed or all stacks; exit code 0 ok, 1 failed, 2 bad input."""
    selected: tuple[str, ...] | None = tuple(stacks) or None
    head = ""
    if last_deployed is not None and selected is None:
        head = head_commit(repo_root)
        since = last_deployed.read_text().strip() if last_deployed.is_file() else None
        selected = changed_stacks(repo_root, since)
        if selected is not None and last_deployed.is_file():
            renewed = stacks_with_secrets_since(repo_root, last_deployed.stat().st_mtime_ns)
            selected = tuple(sorted({*selected, *renewed}))
        print(
            "Deploying " + ("all stacks" if selected is None else ", ".join(selected) or "nothing")
        )

    try:
        plans = discover_stack_plans(repo_root, selected) if selected != () else ()
        if plans:
            plans = changed_first(repo_root, plans, first_changed_since, runner=runner)
            print("In order: " + ", ".join(plan.name for plan in plans), flush=True)
        report = (
            deploy(
                repo_root,
                plans,
                readiness_timeout=readiness_timeout,
                metrics_dir=metrics_dir,
                runner=runner,
            )
            if plans
            else DeploymentReport((), {})
        )
    except (DeploymentInputError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if report.failed:
        return 1
    if head and last_deployed is not None:
        last_deployed.parent.mkdir(parents=True, exist_ok=True)
        telemetry.replace_file(last_deployed, head + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
