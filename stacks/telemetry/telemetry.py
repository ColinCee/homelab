"""The lab's one shared Python module: how every service and host script reports what it does,
reads its secrets and calls HTTP. Call `start()` once, when the program starts, then log through
`logging.getLogger(name)` with values as `extra` fields. Write a metrics file for Alloy, or any
file read while it changes, with `replace_file`; read a secret with `secret`; call a URL with
`http` or `http_json`; trace a job's run with `run` and its steps with `step`, or send spans
timed after the fact with `otlp_span` and `send_traces`. tests/test_telemetry.py fails a second
way to do these.

Each container mounts this file into its Python's site-packages (compose.yaml, the service's
volumes), so services run alone with `python -I` still import it; tests/test_json_logs.py fails a
service that does not. Host scripts find it beside them (ci-runner/install_host.py) or
by a path into the checkout (scripts/deploy.py, deploy_main.py). Each line is one JSON object,
which Alloy reads (config.alloy, loki.process "docker"); an exception is kept under
OpenTelemetry's names.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from typing import Any

_RECORD_FIELDS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonLines(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # A value passed as `extra` never takes the place of these.
        line: dict[str, object] = {
            k: v for k, v in vars(record).items() if k not in _RECORD_FIELDS
        } | {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # The run's trace, under the names Alloy reads out (config.alloy, loki.process "docker").
        if (current := _SPAN.get()) is not None:
            line.setdefault("trace_id", current.trace_id)
            line.setdefault("span_id", current.span_id)
        if record.exc_info and record.exc_info[1]:
            line["exception.type"] = type(record.exc_info[1]).__name__
            line["exception.message"] = str(record.exc_info[1])
            line["exception.stacktrace"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def start() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLines())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def replace_file(path: Path, data: str | bytes, mode: int = 0o644) -> None:
    """Replace `path` whole, so a reader sees the old file or the new, never half of one."""
    # Hidden and not *.prom, so Alloy's textfile collector never reads it half written.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data.encode() if isinstance(data, str) else data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def label(text: str) -> str:
    """Text as a metrics file's label value, escaped as Prometheus reads it."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


SECRETS = Path("/run/secrets")
# Cloudflare refuses Python's own.
USER_AGENT = "homelab/1"
# Sent again after a failure that may have reached the server, as doing them twice does no harm.
_IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "DELETE"})


def secret(name: str | Path) -> str:
    """A Compose secret by name (/run/secrets/<name>), or the secret file at an absolute path,
    without the whitespace or newline around it."""
    return (SECRETS / name).read_text(encoding="utf-8").strip()


def http(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    login: str | None = None,
    timeout: float = 30,
    tries: int = 1,
    limit: int | None = None,
) -> bytes:
    """The body of URL's answer, at most LIMIT bytes, following redirects. LOGIN is `user:password`
    for basic auth. Up to TRIES times: again on 429, and for an idempotent method on a 5xx or a
    failed connection. Raises only OSError: urllib.error.HTTPError for an error status."""
    sent = {"User-Agent": USER_AGENT, **(headers or {})}
    if login:
        sent["Authorization"] = "Basic " + base64.b64encode(login.encode()).decode()
    for left in reversed(range(tries)):
        try:
            request = urllib.request.Request(url, data=data, method=method)
        except ValueError:
            # Its message would print the URL, which may be a secret (a ping URL).
            raise ValueError("not an http(s) URL") from None
        for name, value in sent.items():
            # Never passed on by a redirect, which may lead to another host.
            request.add_unredirected_header(name, value)
        wait = 2.0
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return bytes(response.read(limit))
        except urllib.error.HTTPError as e:
            again = e.code == 429 or (e.code >= 500 and method in _IDEMPOTENT)
            if not (again and left):
                raise
            with contextlib.suppress(ValueError):
                wait = max(0.0, min(float(e.headers.get("Retry-After") or wait), 5.0))
            e.close()
        except (OSError, HTTPException) as e:
            if not (method in _IDEMPOTENT and left):
                if isinstance(e, OSError):
                    raise
                # Only its type: the message of one may carry the URL, as a secret may be one.
                raise OSError(f"{method}: {type(e).__name__}") from e
        time.sleep(wait)
    raise AssertionError("tries must be at least 1")


def http_json(
    url: str,
    *,
    method: str = "GET",
    body: object = None,
    headers: Mapping[str, str] | None = None,
    login: str | None = None,
    timeout: float = 30,
    tries: int = 1,
) -> Any:  # noqa: ANN401 - the server's JSON, read as its API documents it
    """URL's JSON answer, None when it is empty; BODY, unless None, is sent as JSON. As `http`."""
    sent: dict[str, str] = dict(headers or {})
    data = None
    if body is not None:
        sent.setdefault("Content-Type", "application/json")
        data = json.dumps(body).encode()
    raw = http(
        url, method=method, data=data, headers=sent, login=login, timeout=timeout, tries=tries
    )
    return json.loads(raw) if raw else None


# Alloy's OTLP receiver, at its fixed address on host-otlp for host programs; a container on
# observability-otlp sets OTEL_EXPORTER_OTLP_ENDPOINT, OpenTelemetry's own name for it, to
# http://alloy:4318 (stacks/observability/compose.yaml).
HOST_OTLP = "http://10.201.0.10:4318"
# Past this a name is cut: anyone's CI run is traced, with names its author chose.
SPAN_NAME_MAX = 200
# The OpenTelemetry environment carrier: a parent process's span for the child's run.
_TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}")
log = logging.getLogger("telemetry")


def otlp_span(
    trace_id: str,
    span_id: str,
    parent: str,
    name: str,
    start: float,
    end: float,
    **attrs: object,
) -> dict[str, Any]:
    """One span in OTLP's JSON, from Unix seconds."""
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": parent,
        "name": name[:SPAN_NAME_MAX],
        "kind": 1,
        "startTimeUnixNano": str(int(start * 1e9)),
        "endTimeUnixNano": str(int(max(start, end) * 1e9)),
        "attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in attrs.items()],
    }


def otlp_traces(
    by_service: Mapping[str, Sequence[Mapping[str, Any]]], **resource: str
) -> dict[str, Any]:
    """OTLP's JSON body for spans grouped by the service each is filed under."""
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": k, "value": {"stringValue": v}}
                        for k, v in {"service.name": service, **resource}.items()
                    ]
                },
                "scopeSpans": [{"spans": list(spans)}],
            }
            for service, spans in by_service.items()
            if spans
        ]
    }


def send_traces(body: Mapping[str, Any], *, timeout: float = 10) -> None:
    """Post an `otlp_traces` body to Alloy. Raises OSError, as `http` does."""
    url = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", HOST_OTLP).rstrip("/") + "/v1/traces"
    http(
        url,
        method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )


@dataclass
class Span:
    """A span of a job's run, open until its `run` or `step` block ends."""

    trace_id: str
    span_id: str
    parent: str
    name: str
    attrs: dict[str, object]
    start: float = field(default_factory=time.time)
    failed: str | None = None

    def fail(self, why: str) -> None:
        """Mark it failed, as a step that returns a failure rather than raising does."""
        self.failed = why

    def traceparent(self) -> str:
        """This span as a child process's TRACEPARENT, so the child's run joins this trace."""
        return f"00-{self.trace_id}-{self.span_id}-01"


_SPAN: contextvars.ContextVar[Span | None] = contextvars.ContextVar("span", default=None)
_ENDED: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "ended", default=None
)


@contextlib.contextmanager
def _open(span: Span) -> Generator[Span]:
    token = _SPAN.set(span)
    try:
        yield span
    except BaseException as e:
        span.fail(span.failed or type(e).__name__)
        raise
    finally:
        _SPAN.reset(token)
        ended = _ENDED.get()
        if ended is not None:
            done = otlp_span(
                span.trace_id,
                span.span_id,
                span.parent,
                span.name,
                span.start,
                time.time(),
                **span.attrs,
            )
            if span.failed is not None:
                done["status"] = {"code": 2, "message": span.failed}
            ended.append(done)


@contextlib.contextmanager
def run(service: str, name: str, **attrs: object) -> Generator[Span]:
    """One run of a job as one trace, filed under SERVICE and sent to Alloy as the block ends;
    a failed send is logged, never raised. A TRACEPARENT in the environment makes it part of
    that span's trace. Lines logged inside carry its trace_id, which Grafana opens as the trace."""
    parent = _TRACEPARENT.fullmatch(os.environ.get("TRACEPARENT", ""))
    trace_id, parent_id = parent.groups() if parent else (secrets.token_hex(16), "")
    ended: list[dict[str, Any]] = []
    token = _ENDED.set(ended)
    try:
        with _open(Span(trace_id, secrets.token_hex(8), parent_id, name, dict(attrs))) as span:
            yield span
    finally:
        _ENDED.reset(token)
        try:
            send_traces(otlp_traces({service: ended}))
        # ValueError: an endpoint that is no URL.
        except (OSError, ValueError) as e:
            log.warning("trace not sent", extra={"trace_id": trace_id, "error": str(e)})


def command(name: str, argv: Sequence[str], *, timeout: float | None = None) -> bool:
    """Run ARGV as the step NAME, its TRACEPARENT passed on so a traced child joins the run
    (`docker exec -e TRACEPARENT` passes it into a container); True when it exits 0."""
    with step(name) as span:
        environment = {**os.environ, "TRACEPARENT": span.traceparent()}
        try:
            code = subprocess.run(argv, env=environment, timeout=timeout, check=False).returncode
        except (OSError, subprocess.TimeoutExpired) as e:
            span.fail(type(e).__name__)
            log.error("command failed to run", extra={"step": name, "error": str(e)})
            return False
        if code:
            span.fail(f"exit {code}")
            log.error("command failed", extra={"step": name, "exit_code": code})
        return code == 0


@contextlib.contextmanager
def step(name: str, **attrs: object) -> Generator[Span]:
    """A span inside the innermost open `run` or `step`; outside a run, it records nothing."""
    outer = _SPAN.get()
    if outer is None:
        yield Span("0" * 32, "0" * 16, "", name, dict(attrs))
        return
    with _open(Span(outer.trace_id, secrets.token_hex(8), outer.span_id, name, dict(attrs))) as s:
        yield s
