"""Security posture audit for homelab server.

Checks firewall, fail2ban, SSH config, automatic updates, and Docker socket permissions.
Run via: mise run check:security
"""

import subprocess
import sys

from homelab.models import AuditReport, CheckResult, Status


def run_cmd(cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a shell command and return the result."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def check_ufw_active() -> CheckResult:
    """Check if UFW firewall is active."""
    result = run_cmd(["sudo", "ufw", "status"])
    output = result.stdout.strip()
    if "Status: active" in output:
        return CheckResult(
            name="ufw", status=Status.PASS, message="Firewall active", details=output
        )
    return CheckResult(name="ufw", status=Status.FAIL, message="Firewall not active")


def check_fail2ban_running() -> CheckResult:
    """Check if fail2ban is running with sshd jail active."""
    result = run_cmd(["systemctl", "is-active", "fail2ban"])
    if result.stdout.strip() != "active":
        return CheckResult(name="fail2ban", status=Status.FAIL, message="fail2ban not running")

    jail_result = run_cmd(["sudo", "fail2ban-client", "status", "sshd"])
    if jail_result.returncode == 0:
        return CheckResult(
            name="fail2ban",
            status=Status.PASS,
            message="Running with sshd jail active",
        )
    return CheckResult(
        name="fail2ban",
        status=Status.WARN,
        message="Running but sshd jail not found",
    )


def check_ssh_config() -> CheckResult:
    """Check SSH is configured securely (key-only, no root login)."""
    result = run_cmd(["sudo", "sshd", "-T"])
    if result.returncode != 0:
        return CheckResult(name="ssh-config", status=Status.SKIP, message="Cannot read sshd config")

    config = result.stdout.lower()
    issues: list[str] = []

    if "passwordauthentication yes" in config:
        issues.append("Password auth enabled (should be disabled)")
    if "permitrootlogin yes" in config:
        issues.append("Root login permitted (should be no)")

    if issues:
        return CheckResult(
            name="ssh-config",
            status=Status.FAIL,
            message="Insecure SSH configuration",
            details="\n".join(issues),
        )
    return CheckResult(
        name="ssh-config", status=Status.PASS, message="Key-only auth, root login disabled"
    )


def check_unattended_upgrades() -> CheckResult:
    """Check if automatic security updates are enabled."""
    result = run_cmd(["systemctl", "is-active", "unattended-upgrades"])
    if result.stdout.strip() == "active":
        return CheckResult(
            name="auto-updates",
            status=Status.PASS,
            message="Automatic security updates active",
        )
    return CheckResult(
        name="auto-updates",
        status=Status.FAIL,
        message="Automatic updates not running",
    )


def check_docker_socket() -> CheckResult:
    """Check Docker socket permissions."""
    result = run_cmd(["stat", "-c", "%a %U %G", "/var/run/docker.sock"])
    if result.returncode != 0:
        return CheckResult(
            name="docker-socket", status=Status.SKIP, message="Docker socket not found"
        )

    perms = result.stdout.strip()
    if "660 root docker" in perms:
        return CheckResult(
            name="docker-socket",
            status=Status.PASS,
            message=f"Standard permissions ({perms})",
        )
    return CheckResult(
        name="docker-socket",
        status=Status.WARN,
        message=f"Non-standard permissions: {perms}",
    )


def run_security_audit() -> AuditReport:
    """Run all security checks and return a report."""
    checks = [
        check_ufw_active(),
        check_fail2ban_running(),
        check_ssh_config(),
        check_unattended_upgrades(),
        check_docker_socket(),
    ]
    return AuditReport(title="Security Posture Audit", checks=checks)


if __name__ == "__main__":
    report = run_security_audit()
    report.print_report()
    sys.exit(0 if report.passed else 1)
