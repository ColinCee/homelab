"""Files read while they change, secrets and HTTP go only through telemetry (replace_file, secret,
http), and every program importing the module can find it (stacks/telemetry/telemetry.py)."""

import ast
import http.server
import json
import logging
import re
import shlex
import stat
import subprocess
import sys
import threading
import urllib.error
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import telemetry

from conftest import ROOT, linux_only, programs, units

# Before conftest's sent_traces stands in for it.
SEND_TRACES = telemetry.send_traces

MODULE = ROOT / "stacks/telemetry/telemetry.py"
INSTALLED = "/usr/local/lib/homelab-ci/"
# The installer repairs homelab-python3, so it imports nothing from the checkout (install_host.py).
OWN_REPLACE = {
    MODULE,
    ROOT / "ci-runner/install_host.py",
    # Its copy runs in Home Assistant's own Python, which has no shared module.
    ROOT / "stacks/home-assistant/history.py",
}


def test_files_are_replaced_only_through_telemetry() -> None:
    for path in programs():
        if path not in OWN_REPLACE:
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"os\.(replace|rename)\(|\.rename\(", text), (
                f"{path.relative_to(ROOT)}: telemetry.replace_file"
            )


def test_label_values_are_escaped_only_through_telemetry() -> None:
    # Escaping or blanking a backslash by hand is how a second label escaper starts.
    by_hand = r"""replace\(\s*(chr\(92\)|["']\\\\\\\\["'])|re\.sub\(\s*r?["']\[[^]]*\\\\"""
    for path in programs():
        if path != MODULE:
            text = path.read_text(encoding="utf-8")
            assert not re.search(by_hand, text), f"{path.relative_to(ROOT)}: telemetry.label"


def test_a_label_value_reads_back_as_written() -> None:
    assert telemetry.label('a "b" \\ c\nd') == 'a \\"b\\" \\\\ c\\nd'


def imports_telemetry(script: Path) -> bool:
    return bool(re.search(r"^import telemetry\b", script.read_text(encoding="utf-8"), re.MULTILINE))


def test_installed_scripts_find_the_module_beside_them() -> None:
    installer = (ROOT / "ci-runner/install_host.py").read_text(encoding="utf-8")
    assert 'here.parent / "stacks/telemetry/telemetry.py"' in installer
    checked = 0
    for unit in units():
        for line in re.findall(r"^ExecStart=(.*\.py\b.*)$", unit.read_text(encoding="utf-8"), re.M):
            command = shlex.split(line)
            script = next(part for part in command if part.endswith(".py"))
            if script.startswith(INSTALLED) and imports_telemetry(
                next(p for p in programs() if p.name == Path(script).name)
            ):
                checked += 1
                # -I would drop the script's folder, where the module is, from the path.
                assert "-Es" in command, unit.name
    assert checked >= 5


@pytest.mark.parametrize(
    "script",
    [
        "stacks/deploy/deploy.py",
        "stacks/deploy/deploy_main.py",
        "stacks/observability/prometheus/replay.py",
        "stacks/backup/unit_metrics.py",
        "stacks/backup/upkeep.py",
        "stacks/home-assistant/history.py",
        "stacks/observability/failed_units.py",
    ],
)
def test_checkout_scripts_find_the_module_under_any_unit(script: str, tmp_path: Path) -> None:
    # As the oldest deploy-main.service runs them: -I, no PYTHONPATH, run from elsewhere.
    load = f"import runpy; runpy.run_path({str(ROOT / script)!r}, run_name='loaded')"
    done = subprocess.run(
        [sys.executable, "-I", "-c", load], cwd=tmp_path, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("mode", [0o644, 0o600])
def test_a_replaced_file_has_its_mode_and_leaves_no_temporary(tmp_path: Path, mode: int) -> None:
    linux_only("POSIX file modes")
    out = tmp_path / "x.prom"
    out.write_text("old\n")
    telemetry.replace_file(out, "new\n", mode)
    assert out.read_text() == "new\n"
    assert stat.S_IMODE(out.stat().st_mode) == mode
    assert [p.name for p in tmp_path.iterdir()] == ["x.prom"]


def test_a_failed_write_keeps_the_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def full(*_: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(telemetry.os, "replace", full)
    out = tmp_path / "x.prom"
    out.write_text("old\n")
    with pytest.raises(OSError):
        telemetry.replace_file(out, "new\n")
    assert out.read_text() == "old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["x.prom"]


# Each calls HTTP its own way, for the reason given; any other file goes through telemetry.http.
OWN_HTTP = {
    # One kept-open connection per thread and conditional requests, so a poll of about 40 repos
    # every few seconds costs GitHub nothing when nothing changed.
    "ci-runner/supervisor/ci_supervisor.py": "http.client",
    # Runs in Colin's terminal with the OS's certificate store and refuses any redirect of the
    # admin login it sends.
    "tailnet/tailscale_policy.py": "urllib.request",
    # CI runs the base commit's copy alone from a temporary folder (checks/ci.py, run_base_rule),
    # where no shared module can be found.
    "checks/versions_hash.py": "urllib.request",
}
HTTP_MODULES = {"urllib.request", "http.client", "requests", "httpx", "aiohttp"}


def python_files() -> list[Path]:
    return [p for p in programs() if p != MODULE]


def imported(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    froms = {
        f"{n.module}.{a.name}"
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module
        for a in n.names
    }
    return names | froms


def test_http_is_called_only_through_telemetry() -> None:
    for path in python_files():
        name = path.relative_to(ROOT).as_posix()
        used = {m for m in HTTP_MODULES for i in imported(path) if (i + ".").startswith(m + ".")}
        if name in OWN_HTTP:
            assert OWN_HTTP[name] in used, f"{name} no longer calls HTTP itself: drop it"
        else:
            assert not used, f"{name}: {used}; use telemetry.http or http_json"


def test_secret_files_are_read_only_through_telemetry() -> None:
    # A file read whole and trimmed, the way telemetry.secret reads one, from a secret-looking name.
    named = re.compile(r"secret|token|password|credential|private|key_file|login", re.I)
    for path in python_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"strip", "rstrip"}
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Attribute)
                and node.func.value.func.attr in {"read_text", "read_bytes", "read"}
            ):
                continue
            source = ast.unparse(node.func.value.func.value)
            assert not named.search(source), (
                f"{path.relative_to(ROOT)}: {source}; use telemetry.secret"
            )


def test_a_secret_is_read_by_name_or_path_without_its_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(telemetry, "SECRETS", tmp_path)
    (tmp_path / "token").write_text(" t0k\n", encoding="utf-8")
    assert telemetry.secret("token") == "t0k"
    assert telemetry.secret(tmp_path / "token") == "t0k"


# What the test server answers, in turn (status, headers, body), and each request's login.
ANSWERS: list[tuple[int, dict[str, str], bytes]] = []
SEEN: list[str | None] = []
PATHS: list[str] = []


class Server(http.server.BaseHTTPRequestHandler):
    def answer(self) -> None:
        SEEN.append(self.headers.get("Authorization"))
        PATHS.append(self.path)
        code, headers, body = ANSWERS.pop(0)
        self.send_response(code)
        for name, value in {"Content-Length": str(len(body)), **headers}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = answer

    def log_message(self, format: str, *args: object) -> None: ...


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setattr(telemetry.time, "sleep", lambda s: None)
    ANSWERS.clear()
    SEEN.clear()
    PATHS.clear()
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Server)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_json_goes_out_and_comes_back(server: str) -> None:
    ANSWERS[:] = [(200, {}, b'{"ok": 1}'), (204, {}, b"")]
    assert telemetry.http_json(server + "/a", method="POST", body={"x": 1}) == {"ok": 1}
    assert telemetry.http_json(server + "/b") is None


def test_a_login_is_never_passed_on_by_a_redirect(server: str) -> None:
    ANSWERS[:] = [(302, {"Location": "/elsewhere"}, b""), (200, {}, b"body")]
    assert telemetry.http(server + "/start", login="u:p", limit=2) == b"bo"
    assert SEEN == ["Basic dTpw", None]


def test_a_failure_is_tried_again_only_when_that_is_safe(server: str) -> None:
    ANSWERS[:] = [(503, {}, b""), (200, {}, b"got")]
    assert telemetry.http(server, tries=2) == b"got"
    ANSWERS[:] = [(429, {"Retry-After": "1"}, b""), (200, {}, b"sent")]
    assert telemetry.http(server, method="POST", tries=2) == b"sent"
    # A POST that failed on the server may have been done: it is not sent twice.
    ANSWERS[:] = [(500, {}, b""), (200, {}, b"")]
    with pytest.raises(urllib.error.HTTPError):
        telemetry.http(server, method="POST", tries=2)
    ANSWERS[:] = [(404, {}, b""), (200, {}, b"")]
    with pytest.raises(urllib.error.HTTPError):
        telemetry.http(server, tries=2)


def spans_of(body: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Each span in an OTLP body by name, with the service it is filed under."""
    return {
        span["name"]: {**span, "service": resource["resource"]["attributes"][0]["value"]}
        for resource in body["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    }


def test_a_run_is_one_trace_its_steps_nested_and_failures_marked(
    sent_traces: list[Mapping[str, Any]],
) -> None:
    with telemetry.run("job", "nightly", commit="abc") as run:
        with telemetry.step("first"), telemetry.step("inner", stack="x"):
            pass
        with telemetry.step("second") as second:
            second.fail("exit 1")
    [body] = sent_traces
    spans = spans_of(body)
    assert set(spans) == {"nightly", "first", "inner", "second"}
    assert {s["traceId"] for s in spans.values()} == {run.trace_id}
    assert spans["nightly"]["parentSpanId"] == ""
    assert spans["first"]["parentSpanId"] == spans["second"]["parentSpanId"] == run.span_id
    assert spans["inner"]["parentSpanId"] == spans["first"]["spanId"]
    assert spans["second"]["status"] == {"code": 2, "message": "exit 1"}
    assert "status" not in spans["first"]
    assert spans["nightly"]["service"] == {"stringValue": "job"}
    assert {"key": "commit", "value": {"stringValue": "abc"}} in spans["nightly"]["attributes"]


def test_an_exception_fails_its_step_and_run_and_the_trace_still_goes(
    sent_traces: list[Mapping[str, Any]],
) -> None:
    with pytest.raises(KeyError), telemetry.run("job", "run"), telemetry.step("step"):
        raise KeyError("x")
    spans = spans_of(sent_traces[0])
    assert spans["step"]["status"]["message"] == spans["run"]["status"]["message"] == "KeyError"


def test_lines_logged_in_a_run_carry_its_trace_and_others_none() -> None:
    def line() -> dict[str, Any]:
        record = logging.makeLogRecord({"msg": "hi", "levelname": "INFO"})
        return json.loads(telemetry.JsonLines().format(record))

    assert "trace_id" not in line()
    with telemetry.run("job", "run") as run, telemetry.step("step") as step:
        assert line()["trace_id"] == run.trace_id
        assert line()["span_id"] == step.span_id
    assert "trace_id" not in line()


def test_a_child_process_run_joins_its_parent_s_trace(
    sent_traces: list[Mapping[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    with telemetry.run("upkeep", "upkeep") as parent, telemetry.step("restore test") as step:
        monkeypatch.setenv("TRACEPARENT", step.traceparent())
        with telemetry.run("backup", "restore test"):
            pass
    child = spans_of(sent_traces[0])["restore test"]
    assert child["traceId"] == parent.trace_id
    assert child["parentSpanId"] == step.span_id


def test_a_failed_send_is_logged_and_the_job_goes_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def down(body: object, **_: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(telemetry, "send_traces", down)
    with telemetry.run("job", "run"):
        pass
    assert "trace not sent" in caplog.text


def test_a_command_is_a_step_its_child_can_join(
    sent_traces: list[Mapping[str, Any]], tmp_path: Path
) -> None:
    seen = tmp_path / "traceparent"
    write = f"import os, pathlib; pathlib.Path({str(seen)!r}).write_text(os.environ['TRACEPARENT'])"
    with telemetry.run("job", "run"):
        assert telemetry.command("ok", [sys.executable, "-c", write])
        assert not telemetry.command("fails", [sys.executable, "-c", "raise SystemExit(3)"])
        assert not telemetry.command("missing", [str(tmp_path / "no-such-program")])
    spans = spans_of(sent_traces[0])
    assert seen.read_text() == f"00-{spans['ok']['traceId']}-{spans['ok']['spanId']}-01"
    assert spans["fails"]["status"]["message"] == "exit 3"
    assert "status" in spans["missing"] and "status" not in spans["ok"]


def test_steps_outside_a_run_record_nothing(sent_traces: list[Mapping[str, Any]]) -> None:
    with telemetry.step("alone") as alone:
        alone.fail("no run")
    assert sent_traces == []


def test_traces_go_to_alloy_s_otlp_path(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", server + "/")
    ANSWERS[:] = [(200, {}, b"")]
    SEND_TRACES(telemetry.otlp_traces({"job": [telemetry.otlp_span("t", "s", "", "n", 1, 2)]}))
    assert PATHS == ["/v1/traces"]


def test_spans_are_built_and_sent_only_through_telemetry() -> None:
    # A second builder or sender drifts from the first: the store's names, the receiver's address.
    for path in python_files():
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"resourceSpans|/v1/traces|:4318\b", text), path.relative_to(ROOT)
