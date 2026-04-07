"""Tests for the agent service."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import app
from review import LLMComment, ReviewResult, Severity, Verdict


def _client():
    return TestClient(app)


def test_health():
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@patch("main.review_pr", new_callable=AsyncMock)
def test_review_returns_202_accepted(mock_review):
    mock_review.return_value = ReviewResult(
        summary="Looks good",
        verdict=Verdict.approve,
        comments=[],
        metadata={"model": "gpt-5.4", "elapsed_seconds": 1.5},
    )
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


def test_review_result_with_inline_comments():
    """Test that inline comments are properly formatted for GitHub API."""
    result = ReviewResult(
        summary="Found issues",
        verdict=Verdict.request_changes,
        comments=[
            LLMComment(
                path="src/main.py",
                line=42,
                severity=Severity.blocker,
                body="This will crash on None input",
            ),
            LLMComment(
                path="src/utils.py",
                line=10,
                severity=Severity.suggestion,
                body="Consider using a constant here",
                start_line=8,
            ),
        ],
        metadata={"model": "gpt-5.4"},
    )
    gh = result.to_github_review()
    assert gh["event"] == "REQUEST_CHANGES"
    assert len(gh["comments"]) == 2
    assert gh["comments"][0]["path"] == "src/main.py"
    assert gh["comments"][0]["line"] == 42
    assert "Blocker" in gh["comments"][0]["body"]
    assert gh["comments"][1]["start_line"] == 8
    assert "Suggestion" in gh["comments"][1]["body"]
