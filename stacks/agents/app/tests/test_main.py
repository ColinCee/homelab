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


@patch("main.review_pr", new_callable=AsyncMock)
def test_review_rejects_duplicate_in_flight(mock_review):
    """A second review for the same PR while one is in-flight should be rejected."""
    from main import _review_status

    _review_status["user/repo#42"] = {
        "status": "in_progress",
        "repo": "user/repo",
        "pr_number": 42,
    }
    try:
        resp = _client().post(
            "/review",
            json={"repo": "user/repo", "pr_number": 42},
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "already_in_progress"
        mock_review.assert_not_called()
    finally:
        _review_status.pop("user/repo#42", None)


# --- Implement endpoint tests ---


@patch("main.implement_issue", new_callable=AsyncMock)
def test_implement_returns_202_accepted(mock_impl):
    mock_impl.return_value = {"pr_number": 99, "elapsed_seconds": 5.0}
    resp = _client().post(
        "/implement",
        json={"repo": "user/repo", "issue_number": 10},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["issue_number"] == 10


def test_implement_missing_fields():
    resp = _client().post("/implement", json={"issue_number": 1})
    assert resp.status_code == 422


def test_implement_status_not_found():
    resp = _client().get("/implement/99999")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_found"


@patch("main.implement_issue", new_callable=AsyncMock)
def test_implement_rejects_duplicate_in_flight(mock_impl):
    from main import _implement_status

    _implement_status["user/repo#10"] = {
        "status": "in_progress",
        "repo": "user/repo",
        "issue_number": 10,
    }
    try:
        resp = _client().post(
            "/implement",
            json={"repo": "user/repo", "issue_number": 10},
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "already_in_progress"
        mock_impl.assert_not_called()
    finally:
        _implement_status.pop("user/repo#10", None)


# --- Fix endpoint tests ---


@patch("main.fix_pr", new_callable=AsyncMock)
def test_fix_returns_202_accepted(mock_fix):
    mock_fix.return_value = {"status": "fixed", "elapsed_seconds": 3.0}
    resp = _client().post(
        "/fix",
        json={"repo": "user/repo", "pr_number": 99},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["pr_number"] == 99


def test_fix_missing_fields():
    resp = _client().post("/fix", json={"pr_number": 1})
    assert resp.status_code == 422
