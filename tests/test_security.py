"""Tests for homelab.security module."""

import subprocess
from unittest.mock import patch

from homelab.models import Status
from homelab.security import (
    check_docker_socket,
    check_fail2ban_running,
    check_ssh_config,
    check_ufw_active,
    check_unattended_upgrades,
    run_security_audit,
)


def _mock_result(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class TestCheckUfwActive:
    @patch("homelab.security.run_cmd")
    def test_active(self, mock_run):
        mock_run.return_value = _mock_result(
            stdout="Status: active\nTo Action From\n-- ------ ----"
        )
        result = check_ufw_active()
        assert result.status == Status.PASS

    @patch("homelab.security.run_cmd")
    def test_inactive(self, mock_run):
        mock_run.return_value = _mock_result(stdout="Status: inactive")
        result = check_ufw_active()
        assert result.status == Status.FAIL


class TestCheckFail2ban:
    @patch("homelab.security.run_cmd")
    def test_running_with_jail(self, mock_run):
        def side_effect(cmd, **_):
            if "is-active" in cmd:
                return _mock_result(stdout="active")
            if "sshd" in cmd:
                return _mock_result(stdout="Status for the jail: sshd", returncode=0)
            return _mock_result()

        mock_run.side_effect = side_effect
        result = check_fail2ban_running()
        assert result.status == Status.PASS

    @patch("homelab.security.run_cmd")
    def test_not_running(self, mock_run):
        mock_run.return_value = _mock_result(stdout="inactive")
        result = check_fail2ban_running()
        assert result.status == Status.FAIL

    @patch("homelab.security.run_cmd")
    def test_running_no_jail(self, mock_run):
        def side_effect(cmd, **_):
            if "is-active" in cmd:
                return _mock_result(stdout="active")
            return _mock_result(returncode=1)

        mock_run.side_effect = side_effect
        result = check_fail2ban_running()
        assert result.status == Status.WARN


class TestCheckSshConfig:
    @patch("homelab.security.run_cmd")
    def test_secure(self, mock_run):
        mock_run.return_value = _mock_result(
            stdout="passwordauthentication no\npermitroot login no\nport 22"
        )
        result = check_ssh_config()
        assert result.status == Status.PASS

    @patch("homelab.security.run_cmd")
    def test_password_auth_enabled(self, mock_run):
        mock_run.return_value = _mock_result(
            stdout="passwordauthentication yes\npermitroot login no"
        )
        result = check_ssh_config()
        assert result.status == Status.FAIL
        assert "Password auth" in (result.details or "")

    @patch("homelab.security.run_cmd")
    def test_config_unreadable(self, mock_run):
        mock_run.return_value = _mock_result(returncode=1)
        result = check_ssh_config()
        assert result.status == Status.SKIP


class TestCheckUnattendedUpgrades:
    @patch("homelab.security.run_cmd")
    def test_active(self, mock_run):
        mock_run.return_value = _mock_result(stdout="active")
        result = check_unattended_upgrades()
        assert result.status == Status.PASS

    @patch("homelab.security.run_cmd")
    def test_inactive(self, mock_run):
        mock_run.return_value = _mock_result(stdout="inactive")
        result = check_unattended_upgrades()
        assert result.status == Status.FAIL


class TestCheckDockerSocket:
    @patch("homelab.security.run_cmd")
    def test_standard_perms(self, mock_run):
        mock_run.return_value = _mock_result(stdout="660 root docker")
        result = check_docker_socket()
        assert result.status == Status.PASS

    @patch("homelab.security.run_cmd")
    def test_nonstandard_perms(self, mock_run):
        mock_run.return_value = _mock_result(stdout="666 root root")
        result = check_docker_socket()
        assert result.status == Status.WARN

    @patch("homelab.security.run_cmd")
    def test_no_socket(self, mock_run):
        mock_run.return_value = _mock_result(returncode=1)
        result = check_docker_socket()
        assert result.status == Status.SKIP


class TestRunSecurityAudit:
    @patch("homelab.security.run_cmd")
    def test_returns_report(self, mock_run):
        mock_run.return_value = _mock_result(stdout="active\nStatus: active")
        report = run_security_audit()
        assert report.title == "Security Posture Audit"
        assert len(report.checks) == 5
