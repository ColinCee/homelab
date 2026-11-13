"""The read-only gateway (stacks/observability/gateway) lets each reader read only read paths,
sends each to its store, and refuses the rest. Runs its start script and the Caddy version
compose.yaml pins (mise.toml) against stand-in stores."""

import base64
import http.client
import http.server
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import STACKS, tool

STACK = STACKS / "observability"

PASSWORDS = {"stats": "s", "agents": "a"}
# Removed when the tests end.
SECRETS = tempfile.TemporaryDirectory()
LOGS: list[Path] = []
STORES = ("prometheus", "alertmanager", "logs", "traces")
QUERY = "/prometheus/api/v1/query?query=up"


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}@homelab.internal:{password}".encode()).decode()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def store(name: str) -> http.server.ThreadingHTTPServer:
    """Answers every request with its own name and the path it got."""

    class Store(http.server.BaseHTTPRequestHandler):
        def answer(self) -> None:
            body = json.dumps({"store": name, "path": self.path}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = answer

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Store)
    # A short poll, so shutdown() returns at once.
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
    return server


def run_gateway(port: int, upstreams: dict[str, int], **passwords: str) -> subprocess.Popen[bytes]:
    """The gateway's own start script and Caddy, with the stores at local stand-ins; Caddy's log
    is `caddy.log` beside its secrets. The container's limits are compose.yaml's
    (tests/test_observability_compose.py)."""
    secrets = Path(tempfile.mkdtemp(dir=SECRETS.name))
    LOGS.append(secrets / "caddy.log")
    for name, password in passwords.items():
        (secrets / f"{name}_password").write_text(password, encoding="utf-8")
    env = {
        "PATH": f"{Path(tool('caddy')).parent}{os.pathsep}{os.environ['PATH']}",
        # Caddy's own files, as /data and /config in the container.
        "XDG_DATA_HOME": str(secrets),
        "XDG_CONFIG_HOME": str(secrets),
        "SECRETS_DIR": str(secrets),
        "CADDYFILE": str(STACK / "gateway/Caddyfile"),
        "GATEWAY_PORT": str(port),
    } | {name.upper(): f"127.0.0.1:{p}" for name, p in upstreams.items()}
    script = str(STACK / "gateway/start.sh")
    # A file, not a pipe nobody reads, which a chatty Caddy would fill and block on.
    with LOGS[-1].open("w") as log:
        return subprocess.Popen(["sh", script], env=env, stdout=log, stderr=log)


def last_log() -> str:
    return LOGS[-1].read_text(encoding="utf-8")[-2000:]


def ask(port: int, method: str, path: str, auth: str | None) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path, headers={"Authorization": auth} if auth else {})
    response = connection.getresponse()
    body = response.read()
    return response.status, json.loads(body) if response.status == 200 else {}


@pytest.fixture(scope="module")
def gateway() -> Iterator[int]:
    servers = {name: store(name) for name in STORES}
    port = free_port()
    upstreams = {name: s.server_address[1] for name, s in servers.items()}
    gateway = run_gateway(port, upstreams, **PASSWORDS)
    try:
        for _ in range(100):
            try:
                if ask(port, "GET", "/", None)[0] == 403:
                    break
            except OSError:
                pass
            if gateway.poll() is not None:
                break
            time.sleep(0.1)
        else:
            gateway.kill()
            pytest.fail("the gateway did not answer in 10 s: " + last_log())
        assert gateway.poll() is None, last_log()
        yield port
    finally:
        gateway.kill()
        gateway.wait(timeout=30)
        for s in servers.values():
            s.shutdown()


# What Grafana and agents read, by store: (store, method, path as sent, path the store gets).
READS = [
    ("prometheus", "GET", QUERY, "/api/v1/query?query=up"),
    ("prometheus", "POST", "/prometheus/api/v1/query_range", "/api/v1/query_range"),
    ("prometheus", "GET", "/prometheus/api/v1/label/job/values", "/api/v1/label/job/values"),
    ("prometheus", "GET", "/prometheus/api/v1/rules", "/api/v1/rules"),
    ("prometheus", "GET", "/prometheus/api/v1/status/buildinfo", "/api/v1/status/buildinfo"),
    ("alertmanager", "GET", "/alertmanager/api/v2/alerts", "/api/v2/alerts"),
    ("alertmanager", "GET", "/alertmanager/api/v2/silences", "/api/v2/silences"),
    ("logs", "POST", "/logs/select/logsql/query", "/select/logsql/query"),
    ("logs", "GET", "/logs/select/logsql/field_values?field=x", None),
    ("logs", "GET", "/logs/select/logsql/stats_query_range", None),
    ("traces", "GET", "/traces/select/jaeger/api/services", "/select/jaeger/api/services"),
    ("traces", "GET", "/traces/select/jaeger/api/traces/0af7651916cd43dd", None),
    ("traces", "GET", "/traces/select/jaeger/api/services/web/operations", None),
    ("traces", "GET", "/traces/select/logsql/query", "/select/logsql/query"),
]

# Writes, admin calls and anything not listed, as the agents' login.
REFUSED = [
    ("POST", "/prometheus/api/v1/write"),
    ("POST", "/prometheus/api/v1/admin/tsdb/delete_series?match[]=up"),
    ("GET", "/prometheus/api/v1/admin/tsdb/snapshot"),
    ("POST", "/prometheus/-/reload"),
    ("GET", "/prometheus/-/quit"),
    ("PUT", QUERY),
    ("PATCH", QUERY),
    ("DELETE", QUERY),
    ("GET", "/prometheus/api/v1/query/../admin/tsdb/snapshot"),
    ("GET", "/prometheus/api/v1/%2e%2e/admin/tsdb/snapshot"),
    ("GET", "/prometheus/api/v1/label/x/../../../-/quit/values"),
    ("GET", "//prometheus/api/v1/query"),
    ("GET", "/PROMETHEUS/api/v1/query"),
    ("HEAD", QUERY),
    ("POST", "/alertmanager/api/v2/silences"),
    ("DELETE", "/alertmanager/api/v2/silence/x"),
    ("POST", "/alertmanager/api/v2/alerts"),
    ("POST", "/alertmanager/-/reload"),
    ("GET", "/"),
    ("GET", "/api/v1/query"),
    ("POST", "/logs/insert/jsonline"),
    ("POST", "/logs/insert/opentelemetry/v1/logs"),
    ("PATCH", "/logs/insert/jsonline"),
    ("GET", "/logs/insert/jsonline"),
    ("GET", "/logs/internal/force_merge"),
    ("GET", "/logs/delete/run_task?filter=x"),
    ("GET", "/logs/select/logsql/../../insert/jsonline"),
    ("GET", "/logs/select/vmui"),
    ("POST", "/traces/insert/opentelemetry/v1/traces"),
    ("GET", "/traces/insert/opentelemetry/v1/traces"),
    ("GET", "/traces/internal/force_flush"),
    ("GET", "/traces/select/jaeger/api/services/../../../../insert/x/operations"),
    ("GET", "/traces/select/vmui"),
]


def test_agents_read_every_store_and_each_request_reaches_its_store(gateway: int) -> None:
    for name, method, path, sent in READS:
        status, got = ask(gateway, method, path, basic("agents", PASSWORDS["agents"]))
        assert status == 200, (method, path)
        # The store's prefix is taken off.
        assert got == {"store": name, "path": sent or "/" + path.split("/", 2)[2]}


def test_every_write_admin_call_or_unlisted_path_is_refused(gateway: int) -> None:
    for method, path in REFUSED:
        assert ask(gateway, method, path, basic("agents", PASSWORDS["agents"]))[0] == 403, path


def test_public_stats_read_one_query_only(gateway: int) -> None:
    stats = basic("stats", PASSWORDS["stats"])
    assert ask(gateway, "GET", QUERY, stats) == (200, {"store": "prometheus", "path": QUERY[11:]})
    for _, method, path, _ in READS[1:]:
        assert ask(gateway, method, path, stats)[0] == 403, path


@pytest.mark.parametrize(
    "auth", [None, basic("agents", "wrong"), basic("agents", ""), "Basic ", basic("root", "a")]
)
def test_a_wrong_login_is_refused(gateway: int, auth: str | None) -> None:
    assert ask(gateway, "GET", QUERY, auth)[0] == 403


def test_a_missing_password_stops_the_gateway() -> None:
    # An empty password would make the login `stats@homelab.internal:` match.
    gateway = run_gateway(free_port(), {}, **(PASSWORDS | {"stats": ""}))
    assert gateway.wait(timeout=30) != 0, last_log()
