"""The lab's one shared Python module: how every service and host script reports what it does,
reads its secrets and calls HTTP. Call `start()` once, when the program starts, then log through
`logging.getLogger(name)` with values as `extra` fields. Write a metrics file for Alloy, or any
file read while it changes, with `replace_file`; read a secret with `secret`; call a URL with
`http` or `http_json`. tests/test_telemetry.py fails a second way to do these.

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
import json
import logging
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
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
