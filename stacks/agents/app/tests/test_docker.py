"""Tests for the Docker container spawning service."""

import asyncio
import os
from unittest.mock import AsyncMock, patch

from services.docker import (
    _worker_name,
    cleanup_orphaned_workers,
    get_own_image,
    is_worker_running,
    parse_worker_result,
    spawn_worker,
    stop_worker,
)


def test_worker_name_format():
    assert _worker_name("review", 42) == "worker-review-42"
    assert _worker_name("implement", 10) == "worker-implement-10"


def test_parse_worker_result_extracts_json():
    logs = '2025-01-01 INFO Starting worker\n{"status": "complete", "premium_requests": 5}\n'
    result = parse_worker_result(logs)
    assert result == {"status": "complete", "premium_requests": 5}


def test_parse_worker_result_uses_last_json_line():
    logs = '{"status": "failed"}\nsome log line\n{"status": "complete", "premium_requests": 3}\n'
    result = parse_worker_result(logs)
    assert result == {"status": "complete", "premium_requests": 3}


def test_parse_worker_result_returns_empty_on_no_json():
    logs = "just some logs\nno json here\n"
    result = parse_worker_result(logs)
    assert result == {}


def test_parse_worker_result_skips_non_dict_json():
    logs = '"just a string"\n[1, 2, 3]\n'
    result = parse_worker_result(logs)
    assert result == {}


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
            env={"WORKER_TASK": "review", "GH_TOKEN": "tok"},
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
            env={"WORKER_TASK": "implement"},
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
def test_cleanup_orphaned_workers_removes_exited(mock_docker):
    mock_docker.side_effect = [
        "worker-review-42 Exited (0) 5 minutes ago",  # ps -a
        "",  # rm
    ]

    asyncio.run(cleanup_orphaned_workers())

    assert mock_docker.call_count == 2
    rm_call = mock_docker.call_args_list[1]
    assert rm_call.args[0] == "rm"
    assert rm_call.args[1] == "worker-review-42"


@patch("services.docker._run_docker", new_callable=AsyncMock)
def test_cleanup_orphaned_workers_skips_running(mock_docker):
    mock_docker.return_value = "worker-review-42 Up 5 minutes"

    asyncio.run(cleanup_orphaned_workers())

    # Only the ps call, no rm
    assert mock_docker.call_count == 1


@patch("services.docker._run_docker", new_callable=AsyncMock)
def test_cleanup_handles_no_docker(mock_docker):
    """Should not crash when Docker is unavailable."""
    mock_docker.side_effect = RuntimeError("docker not found")
    asyncio.run(cleanup_orphaned_workers())  # Should not raise
