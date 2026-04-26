"""Tests for the Docker container spawning service."""

import asyncio
import os
from unittest.mock import AsyncMock, Mock, patch

import pytest

import services.docker as docker_module
from services.docker import (
    _parse_docker_timestamp,
    _parse_worker_name,
    _run_docker,
    _worker_name,
    cleanup_orphaned_workers,
    discover_running_workers,
    get_logs,
    get_own_image,
    is_worker_running,
    parse_worker_result,
    spawn_worker,
    stop_worker,
    wait_container,
)


def test_worker_name_format():
    assert _worker_name("review", 42) == "worker-review-42"
    assert _worker_name("implement", 10) == "worker-implement-10"


def test_parse_worker_name_valid():
    assert _parse_worker_name("worker-review-42") == ("review", 42)
    assert _parse_worker_name("worker-implement-10") == ("implement", 10)


def test_parse_worker_name_invalid():
    assert _parse_worker_name("not-a-worker") is None
    assert _parse_worker_name("worker-review") is None
    assert _parse_worker_name("worker-review-abc") is None
    assert _parse_worker_name("") is None


def test_parse_docker_timestamp():
    ts = "2026-04-14T00:30:00.123456789Z"
    epoch = _parse_docker_timestamp(ts)
    assert isinstance(epoch, float)
    assert epoch > 0


def test_parse_docker_timestamp_no_nanos():
    ts = "2026-04-14T00:30:00Z"
    epoch = _parse_docker_timestamp(ts)
    assert isinstance(epoch, float)
    assert epoch > 0


def test_parse_worker_result_extracts_json():
    logs = '2025-01-01 INFO Starting worker\n{"status": "complete", "premium_requests": 5}\n'
    result = parse_worker_result(logs)
    assert result is not None
    assert result.status == "complete"
    assert result.premium_requests == 5


def test_parse_worker_result_uses_last_json_line():
    logs = '{"status": "failed"}\nsome log line\n{"status": "complete", "premium_requests": 3}\n'
    result = parse_worker_result(logs)
    assert result is not None
    assert result.status == "complete"
    assert result.premium_requests == 3


def test_parse_worker_result_returns_empty_on_no_json():
    logs = "just some logs\nno json here\n"
    result = parse_worker_result(logs)
    assert result is None


def test_parse_worker_result_skips_non_dict_json():
    logs = '"just a string"\n[1, 2, 3]\n'
    result = parse_worker_result(logs)
    assert result is None


@patch("services.docker._run_docker", new_callable=AsyncMock)
def test_get_own_image_from_env(mock_docker):
    """WORKER_IMAGE env var takes priority over Docker inspect."""
    with patch.dict(os.environ, {"WORKER_IMAGE": "my-image:v1"}):
        image = asyncio.run(get_own_image())
    assert image == "my-image:v1"
    mock_docker.assert_not_called()


@patch("services.docker._run_docker", new_callable=AsyncMock, return_value="agent:latest")
def test_get_own_image_from_docker_inspect(mock_docker):
    """Falls back to Docker inspect when WORKER_IMAGE is not set."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("WORKER_IMAGE", None)
        image = asyncio.run(get_own_image())
    assert image == "agent:latest"
    mock_docker.assert_awaited_once()


@patch("services.docker._run_docker", new_callable=AsyncMock)
def test_spawn_worker_calls_docker_run(mock_docker):
    mock_docker.return_value = "container123"

    container_id = asyncio.run(
        spawn_worker(
            task_type="review",
            image="agent:latest",
            env={"TASK_TYPE": "review", "GH_TOKEN": "tok"},
            number=42,
            volumes=["repo-cache:/repo.git"],
        )
    )

    assert container_id == "container123"
    # First call is stop_worker (inspect), second is docker run
    # stop_worker's inspect will raise RuntimeError (no existing container)
    run_call = None
    for c in mock_docker.call_args_list:
        if c.args[0] == "run":
            run_call = c
            break
    assert run_call is not None
    args = run_call.args
    assert "run" in args
    assert "-d" in args
    assert "--name" in args
    assert "worker-review-42" in args
    assert "agent:latest" in args


@patch("services.docker._run_docker", new_callable=AsyncMock)
def test_spawn_worker_stops_existing_first(mock_docker):
    """spawn_worker should attempt to stop any existing worker with the same name."""
    call_log = []

    async def track_calls(*args):
        call_log.append(args[0])
        if args[0] == "inspect":
            return "true"
        return "container456"

    mock_docker.side_effect = track_calls

    asyncio.run(
        spawn_worker(
            task_type="implement",
            image="agent:latest",
            env={"TASK_TYPE": "implement"},
            number=10,
        )
    )

    # Should call inspect, stop, rm (from stop_worker), then run
    assert "inspect" in call_log
    assert "stop" in call_log
    assert "rm" in call_log
    assert "run" in call_log


@patch("services.docker._run_docker", new_callable=AsyncMock)
def test_is_worker_running_returns_true(mock_docker):
    mock_docker.return_value = "true"
    result = asyncio.run(is_worker_running("review", 42))
    assert result is True


@patch("services.docker._run_docker", new_callable=AsyncMock)
def test_is_worker_running_returns_false_when_stopped(mock_docker):
    mock_docker.return_value = "false"
    result = asyncio.run(is_worker_running("review", 42))
    assert result is False


@patch("services.docker._run_docker", new_callable=AsyncMock)
def test_is_worker_running_returns_false_when_not_found(mock_docker):
    mock_docker.side_effect = RuntimeError("no such container")
    result = asyncio.run(is_worker_running("review", 42))
    assert result is False


@patch("services.docker._run_docker", new_callable=AsyncMock)
def test_stop_worker_handles_nonexistent(mock_docker):
    """stop_worker should not raise if the container doesn't exist."""
    mock_docker.side_effect = RuntimeError("no such container")
    asyncio.run(stop_worker("review", 42))  # Should not raise


@patch("services.docker._run_docker", new_callable=AsyncMock)
def test_cleanup_orphaned_workers_removes_exited_and_harvests(mock_docker):
    """Cleanup should harvest logs/timestamps and return info for fallback logging."""
    mock_docker.side_effect = [
        "worker-review-42 Exited (0) 5 minutes ago",  # ps -a
        '{"status": "complete", "premium_requests": 5}\n',  # logs
        "2026-04-14T00:00:00Z 2026-04-14T00:10:00Z",  # inspect timestamps
        "",  # rm
    ]

    harvested = asyncio.run(cleanup_orphaned_workers())

    assert len(harvested) == 1
    assert harvested[0]["task_type"] == "review"
    assert harvested[0]["number"] == 42
    assert "complete" in str(harvested[0]["logs"])
    assert harvested[0]["duration_seconds"] == 600.0

    rm_call = [c for c in mock_docker.call_args_list if c.args[0] == "rm"]
    assert len(rm_call) == 1
    assert rm_call[0].args[1] == "worker-review-42"


@patch("services.docker._run_docker", new_callable=AsyncMock)
def test_cleanup_orphaned_workers_skips_running(mock_docker):
    mock_docker.return_value = "worker-review-42 Up 5 minutes"

    harvested = asyncio.run(cleanup_orphaned_workers())

    assert harvested == []
    assert mock_docker.call_count == 1


@patch("services.docker._run_docker", new_callable=AsyncMock)
def test_cleanup_handles_no_docker(mock_docker):
    """Should not crash when Docker is unavailable."""
    mock_docker.side_effect = RuntimeError("docker not found")
    harvested = asyncio.run(cleanup_orphaned_workers())
    assert harvested == []


@patch("services.docker._run_docker", new_callable=AsyncMock)
def test_cleanup_handles_log_failure_gracefully(mock_docker):
    """Cleanup should still remove containers even if log retrieval fails."""
    mock_docker.side_effect = [
        "worker-implement-10 Exited (1) 2 minutes ago",  # ps -a
        RuntimeError("logs failed"),  # logs
        "2026-04-14T00:00:00Z 2026-04-14T00:05:00Z",  # inspect timestamps
        "",  # rm
    ]

    harvested = asyncio.run(cleanup_orphaned_workers())

    assert len(harvested) == 1
    assert harvested[0]["logs"] == ""
    assert harvested[0]["duration_seconds"] == 300.0


def test_run_docker_kills_process_on_timeout():
    async def communicate() -> tuple[bytes, bytes]:
        await asyncio.sleep(3600)
        return (b"", b"")

    async def run():
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_proc:
            proc = AsyncMock()
            proc.communicate.side_effect = communicate
            proc.kill = Mock()
            proc.wait = AsyncMock(return_value=0)
            mock_proc.return_value = proc

            with (
                patch.object(docker_module, "_DOCKER_COMMAND_TIMEOUT", 0.01),
                pytest.raises(
                    RuntimeError,
                    match=r"Docker command timed out after 0\.01s: docker inspect worker-review-42",
                ),
            ):
                await _run_docker("inspect", "worker-review-42")

        proc.kill.assert_called_once_with()
        proc.wait.assert_awaited_once()

    asyncio.run(run())


def test_get_logs_uses_shorter_timeout():
    async def communicate() -> tuple[bytes, bytes | None]:
        await asyncio.sleep(3600)
        return (b"", None)

    async def run():
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_proc:
            proc = AsyncMock()
            proc.communicate.side_effect = communicate
            proc.kill = Mock()
            proc.wait = AsyncMock(return_value=0)
            mock_proc.return_value = proc

            with (
                patch.object(docker_module, "_DOCKER_LOGS_TIMEOUT", 0.01),
                pytest.raises(
                    RuntimeError,
                    match=r"Docker command timed out after 0\.01s: docker logs container123",
                ),
            ):
                await get_logs("container123")

        proc.kill.assert_called_once_with()
        proc.wait.assert_awaited_once()

    asyncio.run(run())


def test_wait_container_uses_longer_timeout():
    async def run():
        with (
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_proc,
            patch(
                "services.docker._communicate_with_timeout", new_callable=AsyncMock
            ) as mock_communicate,
        ):
            proc = AsyncMock()
            proc.returncode = 0
            mock_proc.return_value = proc
            mock_communicate.return_value = (b"0", b"")

            exit_code = await wait_container("container123")

        assert exit_code == 0
        mock_communicate.assert_awaited_once_with(
            proc,
            timeout_seconds=docker_module._DOCKER_WAIT_TIMEOUT,
            command="docker wait container123",
        )

    asyncio.run(run())


@patch("services.docker._run_docker", new_callable=AsyncMock)
def test_discover_running_workers_finds_containers(mock_docker):
    mock_docker.side_effect = [
        "abc123 worker-review-42",  # ps --filter running
        "2026-04-14T00:00:00Z",  # inspect StartedAt
    ]

    workers = asyncio.run(discover_running_workers())

    assert len(workers) == 1
    assert workers[0]["container_id"] == "abc123"
    assert workers[0]["task_type"] == "review"
    assert workers[0]["number"] == 42
    started_at = workers[0]["started_at"]
    assert isinstance(started_at, float)
    assert started_at > 0


@patch("services.docker._run_docker", new_callable=AsyncMock)
def test_discover_running_workers_returns_empty_when_none(mock_docker):
    mock_docker.return_value = ""

    workers = asyncio.run(discover_running_workers())

    assert workers == []


@patch("services.docker._run_docker", new_callable=AsyncMock)
def test_discover_running_workers_handles_docker_unavailable(mock_docker):
    mock_docker.side_effect = RuntimeError("docker not found")

    workers = asyncio.run(discover_running_workers())

    assert workers == []
