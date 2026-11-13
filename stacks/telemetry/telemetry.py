"""How every Python service in the lab reports what it does: call `start()` once, when the program
starts, then log through `logging.getLogger(name)` with values as `extra` fields. Write a metrics
file for Alloy, or any file read while it changes, with `replace_file`.

Each container mounts this file into its Python's site-packages (compose.yaml, the service's
volumes), so services run alone with `python -I` still import it; tests/test_json_logs.py fails a
service that does not. Host scripts find it beside them (ci-runner/install_host.py) or on
PYTHONPATH (scripts/deploy.sh, deploy-main.service). Lines are one JSON object each, which Alloy
reads (config.alloy, loki.process "docker"); an exception is kept under OpenTelemetry's names.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

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
