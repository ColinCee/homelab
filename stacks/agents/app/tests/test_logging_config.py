"""Tests for shared logging configuration."""

import json
import logging

import pytest

from logging_config import configure_logging, resolve_log_format, set_task_context


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


def test_set_task_context_adds_issue_number_to_json_logs(
    capsys: pytest.CaptureFixture[str],
):
    configure_logging("json")
    set_task_context("implement", 42)

    logging.getLogger("tests.logging").info("hello world")

    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["task_type"] == "implement"
    assert payload["issue_number"] == 42
    assert "pr_number" not in payload


def test_set_task_context_adds_pr_number_to_json_logs(capsys: pytest.CaptureFixture[str]):
    configure_logging("json")
    set_task_context("review", 99)

    logging.getLogger("tests.logging").info("hello world")

    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["task_type"] == "review"
    assert payload["pr_number"] == 99
    assert "issue_number" not in payload


def test_resolve_log_format_rejects_invalid_value():
    with pytest.raises(ValueError, match="LOG_FORMAT must be 'json' or 'text'"):
        resolve_log_format("pretty")
