"""Tests for homelab.health module."""

import subprocess
from unittest.mock import patch

from homelab.health import (
    check_container_running,
    check_http_endpoint,
    check_tcp_port,
    run_health_checks,
)
from homelab.models import Status


def _mock_result(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class TestCheckContainerRunning:
    @patch("homelab.health.run_cmd")
    def test_running_container(self, mock_run):
        mock_run.return_value = _mock_result(stdout="running\n")
        result = check_container_running("homeassistant")
        assert result.status == Status.PASS
        assert result.name == "container:homeassistant"

    @patch("homelab.health.run_cmd")
    def test_stopped_container(self, mock_run):
        mock_run.return_value = _mock_result(stdout="exited\n")
        result = check_container_running("homeassistant")
        assert result.status == Status.FAIL

    @patch("homelab.health.run_cmd", side_effect=FileNotFoundError)
    def test_docker_not_available(self, mock_run):
        result = check_container_running("homeassistant")
        assert result.status == Status.SKIP

    @patch("homelab.health.run_cmd", side_effect=subprocess.TimeoutExpired(cmd="", timeout=10))
    def test_timeout(self, mock_run):
        result = check_container_running("homeassistant")
        assert result.status == Status.FAIL


class TestCheckTcpPort:
    @patch("homelab.health.run_cmd")
    def test_port_open(self, mock_run):
        mock_run.return_value = _mock_result(returncode=0)
        result = check_tcp_port("localhost", 1883, "mqtt")
        assert result.status == Status.PASS

    @patch("homelab.health.run_cmd")
    def test_port_closed(self, mock_run):
        mock_run.return_value = _mock_result(returncode=1)
        result = check_tcp_port("localhost", 1883, "mqtt")
        assert result.status == Status.FAIL

    @patch("homelab.health.run_cmd", side_effect=subprocess.TimeoutExpired(cmd="", timeout=5))
    def test_timeout(self, mock_run):
        result = check_tcp_port("localhost", 1883, "mqtt")
        assert result.status == Status.FAIL


class TestCheckHttpEndpoint:
    @patch("homelab.health.run_cmd")
    def test_endpoint_up(self, mock_run):
        mock_run.return_value = _mock_result(stdout="200", returncode=0)
        result = check_http_endpoint("http://localhost:8123", "ha")
        assert result.status == Status.PASS
        assert "200" in result.message

    @patch("homelab.health.run_cmd")
    def test_endpoint_down(self, mock_run):
        mock_run.return_value = _mock_result(stdout="503", returncode=22)
        result = check_http_endpoint("http://localhost:8123", "ha")
        assert result.status == Status.FAIL


class TestRunHealthChecks:
    @patch("homelab.health.run_cmd")
    def test_returns_report(self, mock_run):
        mock_run.return_value = _mock_result(stdout="running\n", returncode=0)
        report = run_health_checks()
        assert report.title == "Service Health Check"
        assert len(report.checks) > 0
