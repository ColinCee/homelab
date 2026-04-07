"""Tests for Copilot CLI JSON extraction."""

import pytest

from copilot_cli import extract_json


class TestExtractJson:
    def test_raw_json(self):
        text = '{"summary": "LGTM", "verdict": "approve", "comments": []}'
        result = extract_json(text)
        assert result["verdict"] == "approve"

    def test_json_in_code_fence(self):
        text = """Here is the review:

```json
{"summary": "Found issues", "verdict": "request_changes", "comments": []}
```
"""
        result = extract_json(text)
        assert result["verdict"] == "request_changes"

    def test_json_with_surrounding_text(self):
        text = """I've reviewed the PR. Here's my assessment:

{"summary": "Looks good", "verdict": "approve", "comments": []}

That's my review."""
        result = extract_json(text)
        assert result["summary"] == "Looks good"

    def test_raises_on_no_json(self):
        with pytest.raises(ValueError, match="Could not extract JSON"):
            extract_json("This is just plain text with no JSON")

    def test_handles_whitespace(self):
        text = """

  {"summary": "OK", "verdict": "approve", "comments": []}

"""
        result = extract_json(text)
        assert result["summary"] == "OK"
