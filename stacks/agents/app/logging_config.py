"""Shared logging configuration for API and worker entrypoints."""

import logging
import os
from typing import Literal

from pythonjsonlogger.json import JsonFormatter

DEFAULT_LOG_FORMAT = "json"
LOG_FORMAT_ENV_VAR = "LOG_FORMAT"
LogFormat = Literal["json", "text"]
_TEXT_FORMAT = "%(asctime)s %(levelname)s %(message)s"
_JSON_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_HANDLER_MARKER = "_homelab_structured_logging"


def resolve_log_format(value: str | None = None) -> LogFormat:
    """Validate LOG_FORMAT and return the normalized value."""
    normalized = (value or DEFAULT_LOG_FORMAT).strip().lower()
    if normalized == "json":
        return "json"
    if normalized == "text":
        return "text"
    raise ValueError(f"{LOG_FORMAT_ENV_VAR} must be 'json' or 'text', got {value!r}")


def _build_formatter(log_format: LogFormat) -> logging.Formatter:
    if log_format == "text":
        return logging.Formatter(_TEXT_FORMAT)
    return JsonFormatter(
        _JSON_FORMAT,
        rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
    )


def configure_logging(log_format: str | None = None) -> None:
    """Install the repo's root logging handler without disturbing test capture."""
    resolved = resolve_log_format(log_format or os.environ.get(LOG_FORMAT_ENV_VAR))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    existing_handler = next(
        (
            existing
            for existing in root_logger.handlers
            if getattr(existing, _HANDLER_MARKER, False)
        ),
        None,
    )
    if existing_handler is not None:
        root_logger.removeHandler(existing_handler)

    handler = logging.StreamHandler()
    setattr(handler, _HANDLER_MARKER, True)
    root_logger.addHandler(handler)

    handler.setFormatter(_build_formatter(resolved))
