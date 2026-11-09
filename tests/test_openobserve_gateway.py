"""OpenObserve's read-only gateway (stacks/observability/gateway) lets each reader read only its
own paths, with root's login sent on, and refuses the rest. Runs compose.yaml's Caddy image on the
host's network against a stand-in OpenObserve, so it needs Docker on Linux (CI)."""

import base64
import http.client
import http.server
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

STACK = Path(__file__).parent.parent / "stacks/observability"
SERVICE = yaml.safe_load((STACK / "compose.yaml").read_text(encoding="utf-8"))["services"][
    "openobserve-gateway"
]
PASSWORDS = {"root": "r00t", "kaizen": "k", "stats": "s", "agents": "a"}
QUERY = "/api/default/prometheus/api/v1/query?query=up"
RANGE = "/api/default/prometheus/api/v1/query_range?query=up&start=0&end=1&step=60"
ALERTS = "/api/v2/default/alerts"

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not shutil.which("docker"), reason="needs Docker on Linux"
)


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}@homelab.internal:{password}".encode()).decode()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OpenObserve(http.server.BaseHTTPRequestHandler):
    """Answers every request with the path and login it got."""

    def answer(self) -> None:
        body = json.dumps({"path": self.path, "auth": self.headers.get("Authorization")}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = answer

    def log_message(self, *args: object) -> None:
        pass


def run_gateway(
    port: int, upstream: int, remove: bool = True, **passwords: str
) -> subprocess.CompletedProcess[str]:
    """The gateway as compose.yaml runs it, on the host's network."""
    env = {f"OPENOBSERVE_{k.upper()}_PASSWORD": v for k, v in passwords.items()}
    env |= {"GATEWAY_PORT": str(port), "OPENOBSERVE": f"127.0.0.1:{upstream}"}
    command = ["docker", "run", "-d", *(["--rm"] if remove else []), "--network", "host"]
    command += ["--read-only", "--user", "65534:65534"]
    command += ["--cap-drop", "ALL", "--security-opt", "no-new-privileges:true"]
    command += ["--tmpfs", "/data:uid=65534,gid=65534", "--tmpfs", "/config:uid=65534,gid=65534"]
    for name in ("Caddyfile", "start.sh"):
        command += ["-v", f"{STACK / 'gateway' / name}:/etc/caddy/{name}:ro"]
    for k, v in env.items():
        command += ["-e", f"{k}={v}"]
    command += ["--entrypoint", "/bin/sh", SERVICE["image"], "/etc/caddy/start.sh"]
    return subprocess.run(command, capture_output=True, text=True, timeout=300)


@pytest.fixture(scope="module")
def gateway() -> Iterator[int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OpenObserve)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = free_port()
    started = run_gateway(port, server.server_address[1], **PASSWORDS)
    assert started.returncode == 0, started.stderr
    container = started.stdout.strip()
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            pytest.fail("the gateway did not start listening in 20 s")
        yield port
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=60)
        server.shutdown()


def ask(port: int, method: str, path: str, auth: str | None) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path, headers={"Authorization": auth} if auth else {})
    response = connection.getresponse()
    body = response.read()
    return response.status, json.loads(body) if response.status == 200 else {}


@pytest.mark.parametrize(
    ("reader", "method", "path"),
    [
        ("stats", "GET", QUERY),
        ("kaizen", "GET", QUERY),
        ("kaizen", "GET", RANGE),
        ("kaizen", "GET", ALERTS),
        ("kaizen", "GET", ALERTS + "?page_size=100"),
        ("agents", "GET", RANGE),
        ("agents", "GET", ALERTS),
        ("agents", "GET", "/api/default/prometheus/api/v1/label/job/values?match[]=up"),
    ],
)
def test_a_reader_reads_its_paths_with_roots_login_sent_on(
    gateway: int, reader: str, method: str, path: str
) -> None:
    status, got = ask(gateway, method, path, basic(reader, PASSWORDS[reader]))
    assert status == 200
    assert got == {"path": path, "auth": basic("root", PASSWORDS["root"])}


@pytest.mark.parametrize(
    ("reader", "method", "path"),
    [
        # Another reader's paths.
        ("stats", "GET", RANGE),
        ("stats", "GET", ALERTS),
        ("kaizen", "GET", "/api/default/prometheus/api/v1/series?match[]=up"),
        # Writes, and anything not listed.
        ("kaizen", "POST", ALERTS),
        ("kaizen", "PUT", ALERTS + "/3LqoVCD0QoPHJkKv8Ze2TBJl9Ja"),
        ("kaizen", "PATCH", ALERTS + "/3LqoVCD0QoPHJkKv8Ze2TBJl9Ja/trigger"),
        ("agents", "POST", "/api/default/prometheus/api/v1/write"),
        ("agents", "POST", QUERY),
        ("agents", "GET", "/api/default/users"),
        ("agents", "POST", "/api/default/_search"),
        ("agents", "GET", ALERTS + "/../../../api/default/users"),
        ("agents", "GET", ALERTS + "/%2e%2e/%2e%2e/%2e%2e/api/default/users"),
        # Cleaned, this is the query; sent on as it came, it is OpenObserve's URL fetcher.
        ("stats", "GET", "/proxy/default/https:/example.com/x/../../../../../" + QUERY[1:]),
        ("stats", "GET", "/" + QUERY),
        ("stats", "GET", QUERY.upper()),
        ("stats", "HEAD", QUERY),
        ("kaizen", "GET", ALERTS + "/3LqoVCD0QoPHJkKv8Ze2TBJl9Ja"),
        ("kaizen", "GET", ALERTS + "/incidents"),
        ("agents", "GET", "/api/default/prometheus/api/v1/label/x/../../../../users/values"),
    ],
)
def test_a_write_or_another_path_is_refused(
    gateway: int, reader: str, method: str, path: str
) -> None:
    assert ask(gateway, method, path, basic(reader, PASSWORDS[reader]))[0] == 403


@pytest.mark.parametrize(
    "auth",
    [
        None,
        basic("kaizen", "wrong"),
        basic("root", PASSWORDS["root"]),
        basic("kaizen", ""),
        "Basic ",
    ],
)
def test_a_wrong_login_is_refused(gateway: int, auth: str | None) -> None:
    assert ask(gateway, "GET", QUERY, auth)[0] == 403


def test_a_missing_password_stops_the_gateway() -> None:
    # An empty password would make the login `stats@homelab.internal:` match.
    started = run_gateway(free_port(), free_port(), remove=False, **(PASSWORDS | {"stats": ""}))
    container = started.stdout.strip()
    try:
        waited = subprocess.run(
            ["docker", "wait", container], capture_output=True, text=True, timeout=30
        )
        assert waited.stdout.strip() not in ("", "0")
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=60)
