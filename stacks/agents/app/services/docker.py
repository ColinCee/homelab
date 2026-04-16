"""Docker container management — spawns and monitors ephemeral worker containers.

The API uses Docker to run CLI tasks in isolated containers that survive API
restarts (ADR-011). Workers use the same image with a different entrypoint.
"""

import asyncio
import logging
import re
import socket
from contextlib import suppress
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from models import TaskResult

logger = logging.getLogger(__name__)

# Resource limits matching the API container (compose.yaml)
_WORKER_MEMORY = "2g"
_WORKER_CPUS = "2.0"
_DOCKER_COMMAND_TIMEOUT = 60
_DOCKER_LOGS_TIMEOUT = 30
# Worker tasks can legitimately run for much longer than a single Docker RPC.
# Keep monitor waits bounded, but well above the Copilot CLI safety timeout.
_DOCKER_WAIT_TIMEOUT = 3600


async def _communicate_with_timeout(
    proc: asyncio.subprocess.Process,
    *,
    timeout_seconds: float,
    command: str,
) -> tuple[bytes, bytes | None]:
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.CancelledError:
        with suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    except TimeoutError as err:
        with suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise RuntimeError(f"Docker command timed out after {timeout_seconds}s: {command}") from err


async def _run_docker(*args: str) -> str:
    """Run a docker CLI command and return stdout."""
    timeout_seconds = _DOCKER_COMMAND_TIMEOUT
    if args and args[0] == "wait":
        timeout_seconds = _DOCKER_WAIT_TIMEOUT
    command = " ".join(("docker", *args))
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate_with_timeout(
        proc, timeout_seconds=timeout_seconds, command=command
    )
    if proc.returncode != 0:
        details = stderr.decode().strip() if stderr is not None else ""
        raise RuntimeError(f"docker {args[0]} failed (exit {proc.returncode}): {details}")
    return stdout.decode().strip()


async def get_own_image() -> str:
    """Detect the Docker image of the currently running container.

    Uses the container hostname (Docker sets this to the short container ID)
    to inspect our own image. Falls back to WORKER_IMAGE env var.
    """
    import os

    explicit = os.environ.get("WORKER_IMAGE")
    if explicit:
        return explicit

    container_id = socket.gethostname()
    try:
        return await _run_docker("inspect", "--format", "{{.Config.Image}}", container_id)
    except RuntimeError as exc:
        raise RuntimeError(
            "Cannot detect own Docker image. Set WORKER_IMAGE env var "
            "or ensure Docker socket is mounted."
        ) from exc


def _worker_name(task_type: str, number: int) -> str:
    """Generate a deterministic worker container name."""
    return f"worker-{task_type}-{number}"


def _parse_worker_name(name: str) -> tuple[str, int] | None:
    """Parse 'worker-review-42' → ('review', 42). Returns None if invalid."""
    parts = name.split("-", 2)
    if len(parts) != 3 or parts[0] != "worker":
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


def _parse_docker_timestamp(ts: str) -> float:
    """Parse a Docker timestamp (RFC 3339 with nanoseconds) to Unix epoch."""
    # Truncate nanosecond precision to microsecond for datetime.fromisoformat
    ts = re.sub(r"(\.\d{6})\d+", r"\1", ts.strip())
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _is_missing_container_error(exc: RuntimeError) -> bool:
    message = str(exc).lower()
    return "no such container" in message or "no such object" in message


async def spawn_worker(
    *,
    task_type: str,
    image: str,
    env: dict[str, str],
    number: int,
    volumes: list[str] | None = None,
) -> str:
    """Spawn an ephemeral worker container and return its container ID.

    Args:
        task_type: "implement" or "review"
        image: Docker image to use (same as API)
        env: Environment variables to pass to the worker
        number: Issue or PR number (used for naming)
        volumes: Volume mounts in "name:/path" format
    """
    name = _worker_name(task_type, number)

    # Stop any existing worker for the same task (supersession)
    await stop_worker(task_type, number)

    cmd = [
        "run",
        "-d",
        "--name",
        name,
        "--memory",
        _WORKER_MEMORY,
        "--cpus",
        _WORKER_CPUS,
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        # entrypoint.sh needs chown/runuser to drop from root to agent user
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "FOWNER",
        "--cap-add",
        "SETUID",
        "--cap-add",
        "SETGID",
    ]

    for key, value in env.items():
        cmd.extend(["-e", f"{key}={value}"])

    for vol in volumes or []:
        cmd.extend(["-v", vol])

    cmd.extend([image, "python", "-m", "worker"])

    container_id = await _run_docker(*cmd)
    logger.info(
        "Spawned worker container %s (%s) for %s #%d",
        name,
        container_id[:12],
        task_type,
        number,
    )
    return container_id


async def wait_container(container_id: str) -> int:
    """Block until a container exits and return its exit code."""
    result = await _run_docker("wait", container_id)
    return int(result.strip())


async def get_logs(container_id: str) -> str:
    """Retrieve stdout/stderr logs from a container."""
    command = f"docker logs {container_id}"
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "logs",
        container_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await _communicate_with_timeout(
        proc, timeout_seconds=_DOCKER_LOGS_TIMEOUT, command=command
    )
    return stdout.decode().strip()


async def remove_container(container_id: str) -> None:
    """Remove a stopped container."""
    try:
        await _run_docker("rm", container_id)
    except RuntimeError:
        logger.warning("Failed to remove container %s", container_id, exc_info=True)


async def stop_worker(task_type: str, number: int) -> None:
    """Stop a running worker container if it exists."""
    name = _worker_name(task_type, number)
    try:
        # Check if container exists and is running
        state = await _run_docker("inspect", "--format", "{{.State.Running}}", name)
    except RuntimeError as exc:
        if _is_missing_container_error(exc):
            logger.debug("Skipping stop for missing worker %s", name, exc_info=True)
        else:
            logger.warning("Failed to inspect worker %s before stop", name, exc_info=True)
        return

    if state.strip() == "true":
        try:
            await _run_docker("stop", name)
            logger.info("Stopped existing worker %s", name)
        except RuntimeError:
            logger.warning("Failed to stop existing worker %s", name, exc_info=True)
            return

    try:
        await _run_docker("rm", name)
    except RuntimeError as exc:
        if _is_missing_container_error(exc):
            logger.debug("Worker %s disappeared before removal", name, exc_info=True)
        else:
            logger.warning("Failed to remove worker %s after stop", name, exc_info=True)


async def is_worker_running(task_type: str, number: int) -> bool:
    """Check if a worker container is currently running."""
    name = _worker_name(task_type, number)
    try:
        state = await _run_docker("inspect", "--format", "{{.State.Running}}", name)
        return state.strip() == "true"
    except RuntimeError:
        return False


def parse_worker_result(logs: str) -> TaskResult | None:
    """Parse the JSON result line from worker container logs.

    The worker writes a single JSON line to stdout as its last action.
    All other output goes to stderr (Python logging).
    """
    for line in reversed(logs.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return TaskResult.model_validate_json(line)
        except ValidationError:
            continue
    return None


async def cleanup_orphaned_workers() -> list[dict[str, Any]]:
    """Remove stopped worker containers and return their info for metric harvesting.

    Called on startup to clean up containers that weren't reaped because
    the API was restarted while monitoring them. Returns parsed info so
    main.py can record metrics before the data is lost.
    """
    harvested: list[dict[str, Any]] = []
    try:
        output = await _run_docker(
            "ps", "-a", "--filter", "name=worker-", "--format", "{{.Names}} {{.Status}}"
        )
    except RuntimeError:
        logger.warning("Docker not available — skipping orphaned worker cleanup")
        return harvested

    if not output:
        return harvested

    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        name, status = parts[0], parts[1]
        if not status.startswith("Exited"):
            continue

        parsed = _parse_worker_name(name)
        if parsed is None:
            try:
                await _run_docker("rm", name)
            except RuntimeError:
                logger.warning("Failed to remove invalid worker container %s", name, exc_info=True)
            continue

        task_type, number = parsed

        # Harvest logs and timestamps before removing
        logs = ""
        duration = 0.0
        try:
            logs = await _run_docker("logs", name)
        except RuntimeError:
            logger.warning("Failed to retrieve logs for orphaned worker %s", name, exc_info=True)

        try:
            ts_output = await _run_docker(
                "inspect", "--format", "{{.State.StartedAt}} {{.State.FinishedAt}}", name
            )
            ts_parts = ts_output.strip().split()
            if len(ts_parts) == 2:
                started = _parse_docker_timestamp(ts_parts[0])
                finished = _parse_docker_timestamp(ts_parts[1])
                duration = max(0.0, finished - started)
        except Exception:
            logger.warning("Failed to parse duration for orphaned worker %s", name, exc_info=True)

        harvested.append(
            {
                "task_type": task_type,
                "number": number,
                "logs": logs,
                "duration_seconds": duration,
            }
        )

        try:
            await _run_docker("rm", name)
            logger.info("Cleaned up orphaned worker container %s", name)
        except RuntimeError:
            logger.warning("Failed to clean up container %s", name, exc_info=True)

    return harvested


async def discover_running_workers() -> list[dict[str, Any]]:
    """Find running worker containers left from a previous API run.

    Called on startup to reconnect monitors for workers that outlived
    the previous API process. Returns info needed to spawn monitors.
    """
    try:
        output = await _run_docker(
            "ps",
            "--filter",
            "name=worker-",
            "--filter",
            "status=running",
            "--format",
            "{{.ID}} {{.Names}}",
        )
    except RuntimeError:
        return []

    if not output:
        return []

    workers: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        container_id, name = parts[0], parts[1]
        parsed = _parse_worker_name(name)
        if parsed is None:
            continue
        task_type, number = parsed

        started_at = 0.0
        try:
            ts = await _run_docker("inspect", "--format", "{{.State.StartedAt}}", container_id)
            started_at = _parse_docker_timestamp(ts)
        except Exception:
            logger.warning(
                "Failed to determine start time for worker container %s",
                container_id,
                exc_info=True,
            )

        workers.append(
            {
                "container_id": container_id,
                "task_type": task_type,
                "number": number,
                "started_at": started_at,
            }
        )

    return workers
