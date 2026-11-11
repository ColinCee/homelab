"""The read-only gateway (stacks/observability/gateway, and each store's gateway.caddy) lets each
reader read only read paths, sends each to its store, and refuses the rest. Runs compose.yaml's
Caddy image against stand-in stores, so it needs Docker."""

import base64
import functools
import http.client
import http.server
import json
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator

import pytest
import yaml

from tests.test_alert_rules import STACK

SERVICE = yaml.safe_load((STACK / "compose.yaml").read_text(encoding="utf-8"))["services"][
    "gateway"
]
PASSWORDS = {"stats": "s", "agents": "a"}
STORES = ("prometheus", "alertmanager", "logs", "traces")
PAIRS = [("loki", "tempo"), ("victorialogs", "victoriatraces")]
QUERY = "/prometheus/api/v1/query?query=up"


@functools.cache
def docker() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0


pytestmark = pytest.mark.skipif("not docker()", reason="needs Docker")


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}@homelab.internal:{password}".encode()).decode()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Store)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run_gateway(
    port: int, upstreams: dict[str, int], logs: str, traces: str, remove: bool = True, **passwords
) -> subprocess.CompletedProcess[str]:
    """The gateway as compose.yaml runs it, with the stores at the host's stand-ins."""
    env = {f"{k.upper()}_PASSWORD": v for k, v in passwords.items()}
    env |= {name.upper(): f"host.docker.internal:{p}" for name, p in upstreams.items()}
    command = ["docker", "run", "-d", *(["--rm"] if remove else [])]
    command += ["-p", f"127.0.0.1:{port}:5080", "--add-host", "host.docker.internal:host-gateway"]
    command += ["--read-only", "--user", "65534:65534"]
    command += ["--cap-drop", "ALL", "--security-opt", "no-new-privileges:true"]
    command += ["--tmpfs", "/data:uid=65534,gid=65534", "--tmpfs", "/config:uid=65534,gid=65534"]
    for name in ("Caddyfile", "start.sh"):
        command += ["-v", f"{STACK / 'gateway' / name}:/etc/caddy/{name}:ro"]
    command += ["-v", f"{STACK / 'logs' / logs / 'gateway.caddy'}:/etc/caddy/logs.caddy:ro"]
    command += ["-v", f"{STACK / 'traces' / traces / 'gateway.caddy'}:/etc/caddy/traces.caddy:ro"]
    for k, v in env.items():
        command += ["-e", f"{k}={v}"]
    command += ["--entrypoint", "/bin/sh", SERVICE["image"], "/etc/caddy/start.sh"]
    return subprocess.run(command, capture_output=True, text=True, timeout=300)


def ask(port: int, method: str, path: str, auth: str | None) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path, headers={"Authorization": auth} if auth else {})
    response = connection.getresponse()
    body = response.read()
    return response.status, json.loads(body) if response.status == 200 else {}


@pytest.fixture(scope="module", params=PAIRS, ids=lambda pair: "+".join(pair))
def gateway(request: pytest.FixtureRequest) -> Iterator[tuple[int, str, str]]:
    servers = {name: store(name) for name in STORES}
    port = free_port()
    upstreams = {name: s.server_address[1] for name, s in servers.items()}
    started = run_gateway(port, upstreams, *request.param, **PASSWORDS)
    assert started.returncode == 0, started.stderr
    container = started.stdout.strip()
    try:
        for _ in range(100):
            try:
                if ask(port, "GET", "/", None)[0] == 403:
                    break
            except OSError:
                pass
            time.sleep(0.2)
        else:
            logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
            pytest.fail("the gateway did not answer in 20 s: " + logs.stderr[-2000:])
        yield port, *request.param
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=60)
        for s in servers.values():
            s.shutdown()


# What Grafana and agents read, by store: (store, method, path as sent, path the store gets).
READS = {
    "all": [
        ("prometheus", "GET", QUERY, "/api/v1/query?query=up"),
        ("prometheus", "POST", "/prometheus/api/v1/query_range", "/api/v1/query_range"),
        ("prometheus", "GET", "/prometheus/api/v1/label/job/values", "/api/v1/label/job/values"),
        ("prometheus", "GET", "/prometheus/api/v1/rules", "/api/v1/rules"),
        ("prometheus", "GET", "/prometheus/api/v1/status/buildinfo", "/api/v1/status/buildinfo"),
        ("alertmanager", "GET", "/alertmanager/api/v2/alerts", "/api/v2/alerts"),
        ("alertmanager", "GET", "/alertmanager/api/v2/silences", "/api/v2/silences"),
    ],
    "loki": [
        (
            "logs",
            "GET",
            "/logs/loki/api/v1/query_range?query=x",
            "/loki/api/v1/query_range?query=x",
        ),
        ("logs", "GET", "/logs/loki/api/v1/labels", "/loki/api/v1/labels"),
        ("logs", "GET", "/logs/loki/api/v1/label/container_name/values", None),
        ("logs", "GET", "/logs/loki/api/v1/detected_field/a.b/values", None),
    ],
    "victorialogs": [
        ("logs", "POST", "/logs/select/logsql/query", "/select/logsql/query"),
        ("logs", "GET", "/logs/select/logsql/field_values?field=x", None),
        ("logs", "GET", "/logs/select/logsql/stats_query_range", None),
    ],
    "tempo": [
        ("traces", "GET", "/traces/tempo/api/search?q=x", "/tempo/api/search?q=x"),
        ("traces", "GET", "/traces/tempo/api/v2/search/tag/resource.service.name/values", None),
        ("traces", "GET", "/traces/tempo/api/traces/0af7651916cd43dd8448eb211c80319c", None),
        ("traces", "GET", "/traces/tempo/api/echo", "/tempo/api/echo"),
    ],
    "victoriatraces": [
        ("traces", "GET", "/traces/select/jaeger/api/services", "/select/jaeger/api/services"),
        ("traces", "GET", "/traces/select/jaeger/api/traces/0af7651916cd43dd", None),
        ("traces", "GET", "/traces/select/jaeger/api/services/web/operations", None),
        ("traces", "GET", "/traces/select/logsql/query", "/select/logsql/query"),
    ],
}

# Writes, admin calls and anything not listed, by store, as the agents' login.
REFUSED = {
    "all": [
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
    ],
    "loki": [
        ("GET", "/logs/flush"),
        ("GET", "/logs/ingester/shutdown"),
        ("GET", "/logs/config"),
        ("POST", "/logs/loki/api/v1/push"),
        ("POST", "/logs/otlp/v1/logs"),
        ("POST", "/logs/loki/api/v1/delete?query=x"),
        ("GET", "/logs/loki/api/v1/delete"),
        ("GET", "/logs/loki/api/v1/detected_field/../../../../flush/values"),
        ("GET", "/logs/loki/api/v1/tail"),
        ("PUT", "/logs/loki/api/v1/rules/ns"),
    ],
    "victorialogs": [
        ("POST", "/logs/insert/jsonline"),
        ("POST", "/logs/insert/opentelemetry/v1/logs"),
        ("PATCH", "/logs/insert/jsonline"),
        ("GET", "/logs/insert/jsonline"),
        ("GET", "/logs/internal/force_merge"),
        ("GET", "/logs/delete/run_task?filter=x"),
        ("GET", "/logs/select/logsql/../../insert/jsonline"),
        ("GET", "/logs/select/vmui"),
    ],
    "tempo": [
        ("POST", "/traces/v1/traces"),
        ("GET", "/traces/tempo/api/overrides"),
        ("POST", "/traces/tempo/api/overrides"),
        ("GET", "/traces/api/search"),
        ("GET", "/traces/status/config"),
        ("GET", "/traces/tempo/status/config"),
        ("GET", "/traces/tempo/status/usage-stats"),
        ("POST", "/traces/live-store/prepare-partition-downscale"),
        ("DELETE", "/traces/live-store/prepare-downscale"),
        ("POST", "/traces/tempo/api/search"),
        ("GET", "/traces/tempo/api/search/tag/a..b/../../../overrides/values"),
    ],
    "victoriatraces": [
        ("POST", "/traces/insert/opentelemetry/v1/traces"),
        ("GET", "/traces/insert/opentelemetry/v1/traces"),
        ("GET", "/traces/internal/force_flush"),
        ("GET", "/traces/select/jaeger/api/services/../../../../insert/x/operations"),
        ("GET", "/traces/select/vmui"),
    ],
}


def mine(table: dict, logs: str, traces: str) -> list:
    return table["all"] + table[logs] + table[traces]


def test_agents_read_every_store_and_each_request_reaches_its_store(
    gateway: tuple[int, str, str],
) -> None:
    port, logs, traces = gateway
    for name, method, path, sent in mine(READS, logs, traces):
        status, got = ask(port, method, path, basic("agents", PASSWORDS["agents"]))
        assert status == 200, (method, path)
        # The store's prefix is taken off.
        assert got == {"store": name, "path": sent or "/" + path.split("/", 2)[2]}


def test_every_write_admin_call_or_unlisted_path_is_refused(
    gateway: tuple[int, str, str],
) -> None:
    port, logs, traces = gateway
    for method, path in mine(REFUSED, logs, traces):
        assert ask(port, method, path, basic("agents", PASSWORDS["agents"]))[0] == 403, path


def test_public_stats_read_one_query_only(gateway: tuple[int, str, str]) -> None:
    port, logs, traces = gateway
    stats = basic("stats", PASSWORDS["stats"])
    assert ask(port, "GET", QUERY, stats) == (200, {"store": "prometheus", "path": QUERY[11:]})
    for _, method, path, _ in mine(READS, logs, traces)[1:]:
        assert ask(port, method, path, stats)[0] == 403, path


@pytest.mark.parametrize(
    "auth", [None, basic("agents", "wrong"), basic("agents", ""), "Basic ", basic("root", "a")]
)
def test_a_wrong_login_is_refused(gateway: tuple[int, str, str], auth: str | None) -> None:
    assert ask(gateway[0], "GET", QUERY, auth)[0] == 403


def test_a_missing_password_stops_the_gateway() -> None:
    # An empty password would make the login `stats@homelab.internal:` match.
    started = run_gateway(free_port(), {}, *PAIRS[0], remove=False, **(PASSWORDS | {"stats": ""}))
    container = started.stdout.strip()
    try:
        waited = subprocess.run(
            ["docker", "wait", container], capture_output=True, text=True, timeout=30
        )
        assert waited.stdout.strip() not in ("", "0")
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=60)
