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
_TASK_CONTEXT_FILTER_MARKER = "_homelab_task_context_filter"


class TaskContextFilter(logging.Filter):
    """Attach worker task context to log records."""

    def __init__(
        self,
        task_type: str,
        *,
        issue_number: int | None = None,
        pr_number: int | None = None,
    ) -> None:
        super().__init__()
        self.task_type = task_type
        self.issue_number = issue_number
        self.pr_number = pr_number

    def filter(self, record: logging.LogRecord) -> bool:
        record_task_type = getattr(record, "task_type", None)
        if record_task_type is None:
            record.task_type = self.task_type
            record_task_type = self.task_type
        if record_task_type != self.task_type:
            return True
        if self.issue_number is not None and getattr(record, "issue_number", None) is None:
            record.issue_number = self.issue_number
        if self.pr_number is not None and getattr(record, "pr_number", None) is None:
            record.pr_number = self.pr_number
        return True


def _clear_task_context_filter(root_logger: logging.Logger) -> None:
    existing_filter = next(
        (
            existing
            for existing in root_logger.filters
            if getattr(existing, _TASK_CONTEXT_FILTER_MARKER, False)
        ),
        None,
    )
    if existing_filter is not None:
        root_logger.removeFilter(existing_filter)
    for handler in root_logger.handlers:
        existing_filter = next(
            (
                existing
                for existing in handler.filters
                if getattr(existing, _TASK_CONTEXT_FILTER_MARKER, False)
            ),
            None,
        )
        if existing_filter is not None:
            handler.removeFilter(existing_filter)


def set_task_context(task_type: str, number: int) -> None:
    """Attach task context to all subsequent root logger records."""
    root_logger = logging.getLogger()
    _clear_task_context_filter(root_logger)

    task_filter = TaskContextFilter(
        task_type,
        issue_number=number if task_type == "implement" else None,
        pr_number=number if task_type == "review" else None,
    )
    setattr(task_filter, _TASK_CONTEXT_FILTER_MARKER, True)
    root_logger.addFilter(task_filter)
    for handler in root_logger.handlers:
        handler.addFilter(task_filter)


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
    _clear_task_context_filter(root_logger)

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
