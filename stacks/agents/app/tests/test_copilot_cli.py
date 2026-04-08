"""Tests for Copilot CLI stats parsing."""

from copilot_cli import _parse_stats

SAMPLE_OUTPUT = """\
Hello world

Total usage est:        3 Premium requests
API time spent:         12s
Total session time:     15s
Total code changes:     +0 -0
Breakdown by AI model:
 gpt-5.4                  26.4k in, 402 out, 0 cached (Est. 2 Premium requests)
 claude-sonnet-4.6          8.1k in, 150 out, 0 cached (Est. 1 Premium request)
"""


def test_parse_premium_requests():
    stats = _parse_stats(SAMPLE_OUTPUT)
    assert stats["premium_requests"] == 3


def test_parse_timing():
    stats = _parse_stats(SAMPLE_OUTPUT)
    assert stats["api_time"] == 12
    assert stats["session_time"] == 15


def test_parse_models():
    stats = _parse_stats(SAMPLE_OUTPUT)
    assert "gpt-5.4" in stats["models"]
    assert "claude-sonnet-4.6" in stats["models"]
    assert "26.4k in" in stats["models"]["gpt-5.4"]


def test_parse_empty_output():
    stats = _parse_stats("just some text\nno stats here")
    assert stats["premium_requests"] == 0
    assert stats["models"] == {}
