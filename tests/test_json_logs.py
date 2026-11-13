"""Our services' JSON log lines (the JsonLines block each one copies)."""

import json
import logging
import sys
from pathlib import Path

from stacks.observability.incidents.relay import JsonLines

ROOT = Path(__file__).parent.parent
SERVICES = [
    "stacks/backup/backup.py",
    "stacks/deploy/doorbell.py",
    "stacks/observability/incidents/relay.py",
    "stacks/observability/public_stats.py",
    "stacks/observability/grafana/users.py",
]


def block(path: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    start = text.index("# One JSON object per line")
    end = text.index("\n\n\n", text.index("def log_json"))
    return text[start:end]


def test_every_service_has_the_same_copy() -> None:
    first = block(SERVICES[0])
    for path in SERVICES[1:]:
        assert block(path) == first, path


def line(record: logging.LogRecord) -> dict[str, object]:
    out = JsonLines().format(record)
    assert "\n" not in out
    parsed: dict[str, object] = json.loads(out)
    return parsed


def test_a_line_carries_its_fields_and_no_record_internals() -> None:
    logger = logging.getLogger("relay-test")
    record = logger.makeRecord(
        "relay-test", logging.ERROR, __file__, 1, "cannot post", (), None,
        extra={"post": "123", "error": "timed out"},
    )  # fmt: skip
    got = line(record)
    assert got.pop("time")
    assert got == {
        "level": "ERROR",
        "logger": "relay-test",
        "msg": "cannot post",
        "post": "123",
        "error": "timed out",
    }


def test_an_exception_is_kept_by_its_semantic_convention_names() -> None:
    try:
        raise ValueError("bad")
    except ValueError:
        record = logging.getLogger("x").makeRecord(
            "x", logging.ERROR, __file__, 1, "crashed", (), sys.exc_info()
        )
    got = line(record)
    assert got["exception.type"] == "ValueError"
    assert got["exception.message"] == "bad"
    assert "raise ValueError" in str(got["exception.stacktrace"])
