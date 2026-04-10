"""Tests for review orchestrator — specifically the review file parser."""

import json
from pathlib import Path

import pytest

from review import _parse_review_file


@pytest.fixture
def review_file(tmp_path: Path) -> Path:
    return tmp_path / ".copilot-review.json"


def _write(path: Path, data: dict | str) -> None:
    content = data if isinstance(data, str) else json.dumps(data)
    path.write_text(content)


class TestParseReviewFile:
    def test_parses_valid_approve(self, review_file: Path):
        _write(review_file, {"event": "APPROVE", "body": "LGTM", "comments": []})
        result = _parse_review_file(review_file)
        assert result["event"] == "APPROVE"
        assert result["body"] == "LGTM"
        assert result["comments"] == []

    def test_parses_request_changes_with_comments(self, review_file: Path):
        _write(
            review_file,
            {
                "event": "REQUEST_CHANGES",
                "body": "Issues found",
                "comments": [{"path": "main.py", "line": 10, "body": "Bug here"}],
            },
        )
        result = _parse_review_file(review_file)
        assert result["event"] == "REQUEST_CHANGES"
        assert len(result["comments"]) == 1
        assert result["comments"][0]["path"] == "main.py"

    def test_defaults_body_to_empty_string(self, review_file: Path):
        _write(review_file, {"event": "APPROVE"})
        result = _parse_review_file(review_file)
        assert result["body"] == ""

    def test_defaults_comments_to_empty_list(self, review_file: Path):
        _write(review_file, {"event": "COMMENT", "body": "Looks fine"})
        result = _parse_review_file(review_file)
        assert result["comments"] == []

    def test_raises_when_file_missing(self, tmp_path: Path):
        missing = tmp_path / "nope.json"
        with pytest.raises(RuntimeError, match="did not produce"):
            _parse_review_file(missing)

    def test_raises_on_invalid_json(self, review_file: Path):
        review_file.write_text("not json {{{")
        with pytest.raises(RuntimeError, match="not valid JSON"):
            _parse_review_file(review_file)

    def test_raises_on_non_object(self, review_file: Path):
        review_file.write_text('"just a string"')
        with pytest.raises(RuntimeError, match="must be a JSON object"):
            _parse_review_file(review_file)

    def test_raises_on_invalid_event(self, review_file: Path):
        _write(review_file, {"event": "MERGE", "body": "lol"})
        with pytest.raises(RuntimeError, match="Invalid review event"):
            _parse_review_file(review_file)

    def test_raises_on_missing_event(self, review_file: Path):
        _write(review_file, {"body": "no event field"})
        with pytest.raises(RuntimeError, match="Invalid review event"):
            _parse_review_file(review_file)

    def test_raises_on_comment_missing_required_keys(self, review_file: Path):
        _write(
            review_file,
            {"event": "APPROVE", "comments": [{"path": "f.py", "body": "x"}]},
        )
        with pytest.raises(RuntimeError, match="missing required key 'line'"):
            _parse_review_file(review_file)

    def test_strips_markdown_code_fences(self, review_file: Path):
        review_file.write_text(
            '```json\n{"event": "APPROVE", "body": "Clean", "comments": []}\n```'
        )
        result = _parse_review_file(review_file)
        assert result["event"] == "APPROVE"

    def test_handles_multiline_comment_body(self, review_file: Path):
        _write(
            review_file,
            {
                "event": "REQUEST_CHANGES",
                "body": "Issues",
                "comments": [
                    {"path": "a.py", "line": 5, "body": "🚫 **Blocker**\n\nDetailed explanation"}
                ],
            },
        )
        result = _parse_review_file(review_file)
        assert "Blocker" in result["comments"][0]["body"]
