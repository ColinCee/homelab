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
def test_review_returns_202_accepted(mock_review):
    mock_review.return_value = {"model": "gpt-5.4", "elapsed_seconds": 1.5}
    resp = _client().post(
        "/review",
        json={"repo": "user/repo", "pr_number": 1},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["pr_number"] == 1


def test_review_missing_fields():
    resp = _client().post("/review", json={"pr_number": 1})
    assert resp.status_code == 422


def test_review_status_not_found():
    resp = _client().get("/review/99999")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_found"
