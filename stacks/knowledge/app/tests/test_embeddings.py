from unittest.mock import patch

import httpx
import pytest

from knowledge.embeddings import (
    _BATCH_SIZE,
    GITHUB_MODELS_URL,
    MODEL_NAME,
    TOKEN_ENV,
    _parse_response,
    get_embeddings,
)
from knowledge.models import EMBEDDING_DIMENSION

_FAKE_REQUEST = httpx.Request("POST", "https://example.com")


def _fake_embedding(index: int = 0) -> dict:
    return {"embedding": [0.1] * EMBEDDING_DIMENSION, "index": index}


def _ok_response(count: int) -> httpx.Response:
    data = {"data": [_fake_embedding(i) for i in range(count)]}
    return httpx.Response(200, json=data, request=_FAKE_REQUEST)


def test_empty_input_returns_empty() -> None:
    assert get_embeddings([]) == []


def test_get_embeddings_calls_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "test-token")
    response = _ok_response(2)

    with patch("knowledge.embeddings.httpx.post", return_value=response) as mock_post:
        result = get_embeddings(["hello", "world"])

    assert len(result) == 2
    assert len(result[0]) == EMBEDDING_DIMENSION
    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert call_args.kwargs["headers"]["Authorization"] == "Bearer test-token"
    # Verify correct GitHub Models endpoint and model ID
    assert call_args.args[0] == GITHUB_MODELS_URL
    assert call_args.kwargs["json"]["model"] == MODEL_NAME


def test_get_embeddings_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with pytest.raises(RuntimeError, match=TOKEN_ENV):
        get_embeddings(["hello"])


def test_get_embeddings_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "test-token")
    rate_limited = httpx.Response(429, json={"error": "rate limited"}, request=_FAKE_REQUEST)
    ok = _ok_response(1)

    with (
        patch("knowledge.embeddings.httpx.post", side_effect=[rate_limited, ok]) as mock_post,
        patch("knowledge.embeddings.time.sleep"),
    ):
        result = get_embeddings(["hello"])

    assert len(result) == 1
    assert mock_post.call_count == 2


def test_get_embeddings_raises_on_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "test-token")
    bad_request = httpx.Response(400, json={"error": "bad"}, request=_FAKE_REQUEST)

    with (
        patch("knowledge.embeddings.httpx.post", return_value=bad_request),
        pytest.raises(httpx.HTTPStatusError),
    ):
        get_embeddings(["hello"])


def test_parse_response_validates_count() -> None:
    data = {"data": [_fake_embedding(0)]}
    with pytest.raises(ValueError, match="Expected 2 embeddings"):
        _parse_response(data, expected=2)


def test_parse_response_validates_dimension() -> None:
    data = {"data": [{"embedding": [0.1, 0.2], "index": 0}]}
    with pytest.raises(ValueError, match=str(EMBEDDING_DIMENSION)):
        _parse_response(data, expected=1)


def test_parse_response_sorts_by_index() -> None:
    data = {"data": [_fake_embedding(1), _fake_embedding(0)]}
    result = _parse_response(data, expected=2)
    assert len(result) == 2


def test_get_embeddings_retries_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "test-token")
    ok = _ok_response(1)

    with (
        patch(
            "knowledge.embeddings.httpx.post",
            side_effect=[httpx.ConnectError("connection refused"), ok],
        ) as mock_post,
        patch("knowledge.embeddings.time.sleep"),
    ):
        result = get_embeddings(["hello"])

    assert len(result) == 1
    assert mock_post.call_count == 2


def test_get_embeddings_batches_large_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "test-token")
    texts = [f"text-{i}" for i in range(_BATCH_SIZE + 5)]

    batch1_response = _ok_response(_BATCH_SIZE)
    batch2_response = _ok_response(5)

    with patch(
        "knowledge.embeddings.httpx.post",
        side_effect=[batch1_response, batch2_response],
    ) as mock_post:
        result = get_embeddings(texts)

    assert len(result) == _BATCH_SIZE + 5
    assert mock_post.call_count == 2
    # First call has batch_size items, second has the remainder
    first_payload = mock_post.call_args_list[0].kwargs["json"]["input"]
    second_payload = mock_post.call_args_list[1].kwargs["json"]["input"]
    assert len(first_payload) == _BATCH_SIZE
    assert len(second_payload) == 5


def test_get_embeddings_exhausts_retries_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV, "test-token")

    with (
        patch(
            "knowledge.embeddings.httpx.post",
            side_effect=httpx.ReadTimeout("timed out"),
        ),
        patch("knowledge.embeddings.time.sleep"),
        pytest.raises(httpx.ReadTimeout),
    ):
        get_embeddings(["hello"])
