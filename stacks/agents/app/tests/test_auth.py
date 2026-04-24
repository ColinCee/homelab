"""Tests for bearer token authentication on dispatch endpoints."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from auth import require_bearer
from main import app

_TOKEN = "test-bearer-token"
_VALID_BODY = {
    "repo": "user/repo",
    "pr_number": 42,
    "triggered_by": "ColinCee",
    "github_token": "ghs_test_token",
}


@pytest.fixture(autouse=True)
def _enable_real_auth(monkeypatch: pytest.MonkeyPatch):
    """Override the bypass from conftest so we exercise the real dependency."""
    monkeypatch.setenv("AGENT_API_KEY", _TOKEN)
    app.dependency_overrides.pop(require_bearer, None)
    yield


def _client() -> TestClient:
    return TestClient(app)


def test_review_rejects_missing_token():
    resp = _client().post("/review", json=_VALID_BODY)
    assert resp.status_code == 401


def test_review_rejects_invalid_token():
    resp = _client().post(
        "/review", json=_VALID_BODY, headers={"Authorization": "Bearer wrong-token"}
    )
    assert resp.status_code == 401


@patch("main._spawn_monitor")
@patch("main.spawn_worker", new_callable=AsyncMock, return_value="abc123")
@patch("main.get_own_image", new_callable=AsyncMock, return_value="agent:latest")
def test_review_accepts_valid_token(mock_image, mock_spawn, mock_monitor):
    resp = _client().post(
        "/review", json=_VALID_BODY, headers={"Authorization": f"Bearer {_TOKEN}"}
    )
    assert resp.status_code == 202


@patch("main.is_worker_running", new_callable=AsyncMock, return_value=False)
@patch("main._spawn_monitor")
@patch("main.spawn_worker", new_callable=AsyncMock, return_value="def456")
@patch("main.get_own_image", new_callable=AsyncMock, return_value="agent:latest")
def test_implement_rejects_missing_token(mock_image, mock_spawn, mock_monitor, mock_running):
    body = {
        "repo": "user/repo",
        "issue_number": 10,
        "triggered_by": "ColinCee",
        "github_token": "ghs_test_token",
    }
    resp = _client().post("/implement", json=body)
    assert resp.status_code == 401


def test_health_does_not_require_token():
    resp = _client().get("/health")
    assert resp.status_code == 200


def test_review_status_does_not_require_token():
    with patch("main.is_worker_running", new_callable=AsyncMock, return_value=False):
        resp = _client().get("/review/123")
    assert resp.status_code == 200


def test_returns_503_when_server_token_not_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    resp = _client().post(
        "/review", json=_VALID_BODY, headers={"Authorization": f"Bearer {_TOKEN}"}
    )
    assert resp.status_code == 503
