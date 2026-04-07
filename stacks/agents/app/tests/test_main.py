"""Tests for the agent service."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import app
from review import ReviewResult


def _client():
    return TestClient(app)


def test_health():
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@patch("main.review_pr", new_callable=AsyncMock)
def test_review_returns_structured_result(mock_review):
    mock_review.return_value = ReviewResult(
        summary="Looks good",
        verdict="approve",
        comments=[],
        metadata={"model": "gpt-5.4", "elapsed_seconds": 1.5},
    )
    resp = _client().post(
        "/review",
        json={"repo": "user/repo", "pr_number": 1},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["event"] == "APPROVE"
    assert "Looks good" in data["body"]
    assert data["comments"] == []


def test_review_missing_fields():
    resp = _client().post("/review", json={"pr_number": 1})
    assert resp.status_code == 422


def test_review_result_with_inline_comments():
    """Test that inline comments are properly formatted for GitHub API."""
    from review import ReviewComment

    result = ReviewResult(
        summary="Found issues",
        verdict="request_changes",
        comments=[
            ReviewComment(
                path="src/main.py",
                line=42,
                severity="must-fix",
                body="This will crash on None input",
                suggestion="if value is not None:",
            ),
            ReviewComment(
                path="src/utils.py",
                line=10,
                severity="nitpick",
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
    assert "Must Fix" in gh["comments"][0]["body"]
    assert "```suggestion" in gh["comments"][0]["body"]
    assert gh["comments"][1]["start_line"] == 8
    assert "Nitpick" in gh["comments"][1]["body"]
