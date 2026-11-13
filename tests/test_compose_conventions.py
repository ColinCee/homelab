"""Every Compose service keeps the stack conventions (docs/architecture.md, "Deploys")."""

import re
from pathlib import Path

from conftest import dockerfile, services

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


# Each limit is about twice the service's peak over 30 days (container_memory_working_set_bytes,
# max_over_time), so it stops a leak without killing a normal peak. Re-measure before lowering one.
def test_every_service_has_a_memory_limit() -> None:
    for _, name, service in services():
        assert service.get("mem_limit"), f"{name}: mem_limit, about twice its 30-day peak"


# Services that write outside their volumes and tmpfs mounts, so cannot run read-only yet.
WRITABLE = {
    # A tmpfs would hide the image's packages; a volume would keep old ones past an image update.
    "home-assistant/homeassistant": (
        "installs the wiser custom integration's requirement (aioWiserHeatAPI) into the image's "
        "/usr/local/lib/python3.*/site-packages when its container is created"
    ),
}
# Services with no healthcheck, and why. The deploy waits for a healthcheck to pass, so one is
# added with its command tried in the running container.
NO_HEALTHCHECK = {
    # No shell or program in the image to run a check with.
    "observability/logs": "distroless image",
    "observability/traces": "distroless image",
    "observability/blackbox": "distroless image",
    "observability/obi": "distroless image",
    "internal-names/dns": "scratch image",
    "tunnel/cloudflared": "distroless image",
    "observability/alloy": "no wget or curl in the image",
    "flight-tracker/backend": "no wget or curl in the image",
}


def test_services_are_read_only_unless_listed() -> None:
    names = {f"{stack.name}/{name}" for stack, name, _ in services()}
    assert names >= WRITABLE.keys()
    for stack, short, service in services():
        name = f"{stack.name}/{short}"
        if name in WRITABLE:
            assert not service.get("read_only"), f"{name} is read-only now: drop it from WRITABLE"
        else:
            assert service.get("read_only") is True, f"{name}: read_only: true, or say why not"


def test_services_have_a_healthcheck_unless_listed() -> None:
    names = {f"{stack.name}/{name}" for stack, name, _ in services()}
    assert names >= NO_HEALTHCHECK.keys()
    for stack, short, service in services():
        name = f"{stack.name}/{short}"
        has = "healthcheck" in service
        if name in NO_HEALTHCHECK:
            assert not has, f"{name} has a healthcheck now: drop it from NO_HEALTHCHECK"
        else:
            assert has, f"{name}: a healthcheck, or say why not in NO_HEALTHCHECK"


def test_python_services_share_one_image_and_mount_their_code() -> None:
    # Stock python with the script and telemetry mounted; a Dockerfile only adds what the script
    # needs beyond the standard library (restic, cryptography), on the same pinned Python.
    pins = set()
    for stack, name, service in services():
        image = str(service.get("image", ""))
        if "build" in service:
            text = dockerfile(stack, service).read_text(encoding="utf-8")
            base = re.search(r"^FROM (python:\S+)", text, re.M)
            if not base:
                continue
            image = base.group(1)
            assert not re.search(r"^(COPY|ADD) .*\.py\b", text, re.M), f"{name}: mount its code"
        if image.startswith("python:"):
            pins.add(image)
    assert len(pins) == 1, f"one Python image for every service: {sorted(pins)}"
