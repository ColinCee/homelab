"""Tests for the agent service."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import app


def _client():
    return TestClient(app)


def test_health():
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@patch("main.review_pr", new_callable=AsyncMock)
def test_review_accepted(mock_review):
    resp = _client().post(
        "/review",
        json={"repo": "user/repo", "pr_number": 1},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "accepted"
    assert "user/repo#1" in data["message"]


def test_review_missing_fields():
    resp = _client().post("/review", json={"pr_number": 1})
    assert resp.status_code == 422
