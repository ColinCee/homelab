"""Docker container management — spawns and monitors ephemeral worker containers.

The API uses Docker to run CLI tasks in isolated containers that survive API
restarts (ADR-011). Workers use the same image with a different entrypoint.
"""

import asyncio
import json
import logging
import socket

logger = logging.getLogger(__name__)

# Resource limits matching the API container (compose.yaml)
_WORKER_MEMORY = "2g"
_WORKER_CPUS = "2.0"


async def _run_docker(*args: str) -> str:
    """Run a docker CLI command and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker {args[0]} failed (exit {proc.returncode}): {stderr.decode().strip()}"
        )
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
    return await _run_docker("logs", container_id)


async def remove_container(container_id: str) -> None:
    """Remove a stopped container."""
    try:
        await _run_docker("rm", container_id)
    except RuntimeError:
        logger.warning("Failed to remove container %s", container_id)


async def stop_worker(task_type: str, number: int) -> None:
    """Stop a running worker container if it exists."""
    name = _worker_name(task_type, number)
    try:
        # Check if container exists and is running
        state = await _run_docker("inspect", "--format", "{{.State.Running}}", name)
        if state.strip() == "true":
            await _run_docker("stop", name)
            logger.info("Stopped existing worker %s", name)
        # Remove stopped container so name can be reused
        await _run_docker("rm", name)
    except RuntimeError:
        pass  # Container doesn't exist — nothing to stop


async def is_worker_running(task_type: str, number: int) -> bool:
    """Check if a worker container is currently running."""
    name = _worker_name(task_type, number)
    try:
        state = await _run_docker("inspect", "--format", "{{.State.Running}}", name)
        return state.strip() == "true"
    except RuntimeError:
        return False


def parse_worker_result(logs: str) -> dict:
    """Parse the JSON result line from worker container logs.

    The worker writes a single JSON line to stdout as its last action.
    All other output goes to stderr (Python logging).
    """
    for line in reversed(logs.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            result = json.loads(line)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue
    return {}


async def cleanup_orphaned_workers() -> None:
    """Remove any stopped worker containers left from a previous API run.

    Called on startup to clean up containers that weren't reaped because
    the API was restarted while monitoring them.
    """
    try:
        output = await _run_docker(
            "ps", "-a", "--filter", "name=worker-", "--format", "{{.Names}} {{.Status}}"
        )
    except RuntimeError:
        logger.warning("Docker not available — skipping orphaned worker cleanup")
        return

    if not output:
        return

    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        name, status = parts[0], parts[1]
        if status.startswith("Exited"):
            try:
                await _run_docker("rm", name)
                logger.info("Cleaned up orphaned worker container %s", name)
            except RuntimeError:
                logger.warning("Failed to clean up container %s", name)
