"""Tests for the agent service."""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from main import app
from review import LLMComment, LLMReview, ReviewResult, Severity, Verdict, fetch_previous_reviews


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
        verdict=Verdict.approve,
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


class TestFetchPreviousReviews:
    """Tests for fetch_previous_reviews context loading."""

    @patch("review.get_token", return_value="fake-token")
    def test_returns_empty_when_no_bot_reviews(self, _mock_token):
        reviews_resp = httpx.Response(
            200,
            json=[
                {
                    "user": {"login": "human-user"},
                    "id": 1,
                    "state": "APPROVED",
                    "body": "lgtm",
                },
            ],
        )

        async def mock_get(_client, _url, _headers):
            return reviews_resp

        async def run():
            with patch("review._github_get", side_effect=mock_get):
                return await fetch_previous_reviews("owner/repo", 1)

        result = asyncio.run(run())
        assert result == ""

    @patch("review.get_token", return_value="fake-token")
    def test_includes_latest_bot_review_context(self, _mock_token):
        reviews_resp = httpx.Response(
            200,
            json=[
                {
                    "user": {"login": "github-actions[bot]"},
                    "id": 100,
                    "state": "CHANGES_REQUESTED",
                    "body": "Found a bug\n\n---\n🤖 metadata",
                },
            ],
        )
        comments_resp = httpx.Response(
            200,
            json=[
                {
                    "path": "src/app.py",
                    "line": 42,
                    "body": "**🚫 Blocker**\n\nNull check missing",
                },
            ],
        )

        call_count = 0

        async def mock_get(_client, _url, _headers):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return reviews_resp
            return comments_resp

        async def run():
            with patch("review._github_get", side_effect=mock_get):
                return await fetch_previous_reviews("owner/repo", 1)

        result = asyncio.run(run())
        assert "CHANGES_REQUESTED" in result
        assert "Found a bug" in result
        assert "src/app.py:42" in result
        assert "Null check missing" in result
        # Metadata footer should be stripped
        assert "metadata" not in result


class TestPrepareStrictSchema:
    """Tests for _prepare_strict_schema strict mode compliance."""

    def test_schema_has_additional_properties_false(self):
        from copilot import _prepare_strict_schema

        schema = _prepare_strict_schema(LLMReview)
        # Root level
        assert schema["additionalProperties"] is False
        # Nested LLMComment in $defs
        comment_def = schema["$defs"]["LLMComment"]
        assert comment_def["additionalProperties"] is False

    def test_all_properties_are_required(self):
        from copilot import _prepare_strict_schema

        schema = _prepare_strict_schema(LLMReview)
        # Root: summary, verdict, comments all required
        assert set(schema["required"]) == {"summary", "verdict", "comments"}
        # LLMComment: all fields including optional start_line
        comment_def = schema["$defs"]["LLMComment"]
        assert "start_line" in comment_def["required"]

    def test_no_defaults_or_titles(self):
        from copilot import _prepare_strict_schema

        schema = _prepare_strict_schema(LLMReview)
        # Root level cleaned
        assert "title" not in schema
        assert "description" not in schema
        # Properties cleaned
        for prop in schema["properties"].values():
            assert "default" not in prop
            assert "title" not in prop
        # $defs cleaned
        for defn in schema["$defs"].values():
            assert "title" not in defn
            assert "description" not in defn
