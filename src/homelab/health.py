"""Health checks for homelab services.

Verifies that Docker containers are running and endpoints are reachable.
Run via: mise run check:health
"""

import subprocess
import sys

from homelab.models import AuditReport, CheckResult, Status


def run_cmd(cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run a shell command and return the result."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def check_container_running(name: str) -> CheckResult:
    """Check if a Docker container is running."""
    try:
        result = run_cmd(["docker", "inspect", "-f", "{{.State.Status}}", name])
        status_text = result.stdout.strip()
        if status_text == "running":
            return CheckResult(name=f"container:{name}", status=Status.PASS, message="Running")
        return CheckResult(
            name=f"container:{name}",
            status=Status.FAIL,
            message=f"Not running (state: {status_text})",
        )
    except FileNotFoundError:
        return CheckResult(
            name=f"container:{name}",
            status=Status.SKIP,
            message="Docker not available",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=f"container:{name}",
            status=Status.FAIL,
            message="Timed out checking container",
        )


def check_tcp_port(host: str, port: int, label: str) -> CheckResult:
    """Check if a TCP port is accepting connections."""
    try:
        result = run_cmd(["bash", "-c", f"echo > /dev/tcp/{host}/{port}"], timeout=5)
        if result.returncode == 0:
            return CheckResult(
                name=f"tcp:{label}",
                status=Status.PASS,
                message=f"Port {port} reachable on {host}",
            )
        return CheckResult(
            name=f"tcp:{label}",
            status=Status.FAIL,
            message=f"Port {port} not reachable on {host}",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=f"tcp:{label}",
            status=Status.FAIL,
            message=f"Connection to {host}:{port} timed out",
        )


def check_http_endpoint(url: str, label: str) -> CheckResult:
    """Check if an HTTP endpoint responds."""
    try:
        result = run_cmd(["curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}", url], timeout=10)
        code = result.stdout.strip()
        if result.returncode == 0:
            return CheckResult(name=f"http:{label}", status=Status.PASS, message=f"HTTP {code}")
        return CheckResult(
            name=f"http:{label}",
            status=Status.FAIL,
            message=f"HTTP {code}" if code else "Connection failed",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(name=f"http:{label}", status=Status.FAIL, message="Request timed out")


def run_health_checks() -> AuditReport:
    """Run all health checks and return a report."""
    checks: list[CheckResult] = []

    # Container checks
    containers = ["homeassistant", "mqtt"]
    for name in containers:
        checks.append(check_container_running(name))

    # Endpoint checks
    checks.append(check_http_endpoint("http://localhost:8123", "home-assistant"))
    checks.append(check_tcp_port("localhost", 1883, "mqtt"))

    return AuditReport(title="Service Health Check", checks=checks)


if __name__ == "__main__":
    report = run_health_checks()
    report.print_report()
    sys.exit(0 if report.passed else 1)
