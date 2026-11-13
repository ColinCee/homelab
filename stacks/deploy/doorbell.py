"""Answers GitHub's push webhook by ringing the deployer (docs/architecture.md, "Deploys").

Any POST /ring writes the ring file, which deploy-main.path watches. It reads no header or body:
a ring only makes the deployer fetch main from GitHub, so a forged one deploys nothing new.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RING = Path(os.environ.get("RING_FILE", "/ring/rung"))
PORT = 8080
# GitHub's push payloads; read only so closing the socket does not reset the reply away.
BODY_MAX = 1 << 20
log = logging.getLogger("doorbell")


# One JSON object per line, which Alloy reads (config.alloy, loki.process "docker"); an exception
# under OpenTelemetry's names. Each service runs alone with `python -I`, so each has a copy;
# tests/test_json_logs.py keeps them the same.
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


def log_json() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLines())
    logging.basicConfig(level=logging.INFO, handlers=[handler])


class Doorbell(BaseHTTPRequestHandler):
    # Strangers reach this through the tunnel: a slow client is cut off, not waited on.
    timeout = 5

    def do_POST(self) -> None:
        length = min(int(self.headers.get("Content-Length") or 0), BODY_MAX)
        self.rfile.read(length)
        if self.path != "/ring":
            self._answer(404)
            return
        try:
            # Its content changes, so PathModified fires even on a bind mount.
            RING.write_text(f"{time.time()}\n", encoding="utf-8")
        except OSError as e:
            log.error("cannot ring", extra={"error": str(e)})
            self._answer(500)
            return
        self._answer(202)

    def do_GET(self) -> None:
        self._answer(404)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        log.info(
            "answered",
            extra={"client": self.client_address[0], "request": self.requestline, "status": code},
        )

    def log_message(self, format: str, *args: object) -> None:
        log.warning(format, *args, extra={"client": self.client_address[0]})

    def _answer(self, code: int) -> None:
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True


def main() -> None:
    ThreadingHTTPServer(("", PORT), Doorbell).serve_forever()


if __name__ == "__main__":
    log_json()
    main()
