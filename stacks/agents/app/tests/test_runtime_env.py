"""Tests for runtime environment settings."""

import pytest
from pydantic import ValidationError

from runtime_env import ApiSettings, WorkerSettings


def test_api_settings_rejects_missing_vars():
    with pytest.raises(ValidationError):
        ApiSettings()  # ty: ignore[missing-argument]


def test_api_settings_accepts_valid_env():
    settings = ApiSettings(
        github_app_id="123",
        github_app_installation_id="456",
        github_app_key_file="/key.pem",
        copilot_github_token="tok",
        agent_api_key="bearer-token",
    )
    assert settings.github_app_id == "123"
    assert settings.agent_api_key == "bearer-token"
    assert settings.log_format == "json"
    assert settings.model == "gpt-5.4"


def test_worker_settings_accepts_legacy_env_aliases(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WORKER_TASK", "review")
    monkeypatch.setenv("WORKER_REPO", "user/repo")
    monkeypatch.setenv("WORKER_PR_NUMBER", "42")
    monkeypatch.setenv("GH_TOKEN", "ghs_test")

    settings = WorkerSettings()  # ty: ignore[missing-argument]
    assert settings.task_type == "review"
    assert settings.repo == "user/repo"
    assert settings.number == 42
    assert settings.log_format == "json"


def test_worker_settings_prefers_canonical_names(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TASK_TYPE", "implement")
    monkeypatch.setenv("REPO", "user/repo")
    monkeypatch.setenv("NUMBER", "10")
    monkeypatch.setenv("GH_TOKEN", "ghs_test")
    monkeypatch.setenv("WORKER_TASK", "review")  # should be ignored

    settings = WorkerSettings()  # ty: ignore[missing-argument]
    assert settings.task_type == "implement"


def test_worker_settings_reads_log_format(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TASK_TYPE", "review")
    monkeypatch.setenv("REPO", "user/repo")
    monkeypatch.setenv("NUMBER", "42")
    monkeypatch.setenv("GH_TOKEN", "ghs_test")
    monkeypatch.setenv("LOG_FORMAT", "text")

    settings = WorkerSettings()  # ty: ignore[missing-argument]
    assert settings.log_format == "text"


def test_worker_settings_rejects_missing_vars(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TASK_TYPE", raising=False)
    monkeypatch.delenv("WORKER_TASK", raising=False)
    monkeypatch.delenv("REPO", raising=False)
    monkeypatch.delenv("WORKER_REPO", raising=False)
    monkeypatch.delenv("NUMBER", raising=False)
    monkeypatch.delenv("WORKER_ISSUE_NUMBER", raising=False)
    monkeypatch.delenv("WORKER_PR_NUMBER", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    with pytest.raises(ValidationError):
        WorkerSettings()  # ty: ignore[missing-argument]
