from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from knowledge.models import EMBEDDING_DIMENSION, Chunk, Document, SearchResult
from knowledge.search import DEFAULT_RESULT_LIMIT, format_search_results, search


def _search_result(*, content: str, score: float = 0.93, chunk_index: int = 2) -> SearchResult:
    document_id = UUID("00000000-0000-0000-0000-000000000001")
    return SearchResult(
        score=score,
        workspace="notes",
        document=Document(
            id=document_id,
            workspace="notes",
            source_path="docs/adr.md",
            title="ADR 001",
            content_hash="hash-123",
            ingested_at=datetime(2026, 4, 19, tzinfo=UTC),
        ),
        chunk=Chunk(
            id=UUID("00000000-0000-0000-0000-000000000002"),
            document_id=document_id,
            chunk_index=chunk_index,
            content=content,
            embedding=[0.1] * EMBEDDING_DIMENSION,
            created_at=datetime(2026, 4, 19, 0, 5, tzinfo=UTC),
        ),
    )


def test_format_search_results_includes_ranked_result_details() -> None:
    # Arrange
    result = _search_result(content="Line one.\n\nLine two with extra   spaces.")

    # Act
    formatted = format_search_results([result])

    # Assert
    assert formatted == (
        "1. score=0.930 workspace=notes source=docs/adr.md chunk=2\n"
        "   Line one. Line two with extra spaces."
    )


def test_format_search_results_truncates_long_content() -> None:
    # Arrange
    result = _search_result(content="word " * 60)

    # Act
    formatted = format_search_results([result])

    # Assert
    assert formatted.endswith("...")
    assert "word word word" in formatted


def test_format_search_results_handles_empty_results() -> None:
    # Arrange / Act
    formatted = format_search_results([])

    # Assert
    assert formatted == "No matching chunks found."


@patch("knowledge.search.connect")
@patch("knowledge.search.search_chunks", return_value=[])
@patch("knowledge.search.get_embeddings", return_value=[[0.1] * EMBEDDING_DIMENSION])
def test_search_embeds_query_and_uses_default_limit(
    mock_get_embeddings: MagicMock,
    mock_search_chunks: MagicMock,
    mock_connect: MagicMock,
) -> None:
    # Arrange
    conn = MagicMock()
    mock_connect.return_value = conn

    # Act
    search("  vector database  ")

    # Assert
    mock_get_embeddings.assert_called_once_with(["vector database"], token=None)
    mock_search_chunks.assert_called_once_with(
        conn,
        [0.1] * EMBEDDING_DIMENSION,
        workspace=None,
        limit=DEFAULT_RESULT_LIMIT,
    )
    conn.close.assert_called_once_with()


def test_search_rejects_blank_query() -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValueError, match="query must not be blank"):
        search("   ", conn=MagicMock())
