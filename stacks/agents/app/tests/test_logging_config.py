"""Tests for shared logging configuration."""

import json
import logging

import pytest

from logging_config import configure_logging, resolve_log_format


def test_configure_logging_emits_json_lines(capsys: pytest.CaptureFixture[str]):
    configure_logging("json")

    logging.getLogger("tests.logging").info("hello world")

    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "tests.logging"
    assert "timestamp" in payload


def test_configure_logging_supports_text_fallback(capsys: pytest.CaptureFixture[str]):
    configure_logging("text")

    logging.getLogger("tests.logging").info("hello text")

    assert "INFO hello text" in capsys.readouterr().err


def test_resolve_log_format_rejects_invalid_value():
    with pytest.raises(ValueError, match="LOG_FORMAT must be 'json' or 'text'"):
        resolve_log_format("pretty")
