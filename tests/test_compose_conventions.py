"""Every Compose service keeps the stack conventions (docs/architecture.md, "Deploys")."""

import re
from pathlib import Path

from conftest import services

from stacks.observability.probes import ADDRESS

# Docker publishes a port with no address on every interface, the internet's included.
PORT_ADDRESSES = {ADDRESS, "127.0.0.1"}


def test_images_are_pinned_by_digest_unless_built_here() -> None:
    for _, name, service in services():
        if "build" not in service:
            assert re.search(r"@sha256:[0-9a-f]{64}$", service["image"]), f"{name}: pin a digest"


def test_services_restart_unless_stopped() -> None:
    for _, name, service in services():
        assert service.get("restart") == "unless-stopped", name


def test_published_ports_bind_tailscale_or_loopback() -> None:
    for _, name, service in services():
        for port in service.get("ports", []):
            address = str(port).rsplit(":", 2)[0] if str(port).count(":") == 2 else None
            assert address in PORT_ADDRESSES, f"{name}: {port} binds every interface"


def test_tests_read_compose_and_run_git_through_conftest() -> None:
    # One loader and one git helper, so a fix to either reaches every test.
    for path in Path(__file__).parent.glob("test_*.py"):
        if path == Path(__file__):
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"def \w*git\(|\[\s*[\"']git[\"']\s*,", text), (
            f"{path.name}: conftest.git"
        )
        assert not re.search(r"glob\([^)]*compose\.ya?ml|compose\.ya?ml.*read_text", text), (
            f"{path.name}: conftest.stacks"
        )
        assert "tests.conftest" not in text, f"{path.name}: import conftest, as pytest does"
