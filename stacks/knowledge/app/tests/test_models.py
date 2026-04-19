from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from knowledge.models import (
    EMBEDDING_DIMENSION,
    Chunk,
    Document,
    IngestResult,
    SearchResult,
)


def test_chunk_requires_expected_embedding_dimension() -> None:
    # Arrange
    document_id = uuid4()

    # Act / Assert
    with pytest.raises(ValidationError, match=str(EMBEDDING_DIMENSION)):
        Chunk(
            document_id=document_id,
            chunk_index=0,
            content="Paragraph one",
            embedding=[0.1, 0.2],
        )


def test_document_validates_string_inputs() -> None:
    # Arrange
    document_id = uuid4()
    ingested_at = datetime(2026, 4, 19, 0, 0, tzinfo=UTC)

    # Act
    document = Document.model_validate(
        {
            "id": str(document_id),
            "source_path": " docs/adr.md ",
            "title": " ADR 001 ",
            "content_hash": " hash-123 ",
            "ingested_at": ingested_at.isoformat(),
        }
    )

    # Assert
    assert document.id == document_id
    assert document.source_path == "docs/adr.md"
    assert document.title == "ADR 001"
    assert document.content_hash == "hash-123"
    assert document.ingested_at == ingested_at


def test_search_result_serializes_nested_models() -> None:
    # Arrange
    document_id = uuid4()
    chunk_id = uuid4()
    ingested_at = datetime(2026, 4, 19, 0, 0, tzinfo=UTC)
    created_at = datetime(2026, 4, 19, 0, 5, tzinfo=UTC)
    embedding = [0.1] * EMBEDDING_DIMENSION
    document = Document(
        id=document_id,
        source_path="docs/adr.md",
        title="ADR 001",
        content_hash="hash-123",
        ingested_at=ingested_at,
    )
    chunk = Chunk(
        id=chunk_id,
        document_id=document_id,
        chunk_index=1,
        content="Architecture decision",
        embedding=embedding,
        metadata={"section": "decision"},
        created_at=created_at,
    )
    result = SearchResult(score=0.93, document=document, chunk=chunk)

    # Act
    dumped = result.model_dump(mode="json")

    # Assert
    assert dumped["score"] == 0.93
    assert dumped["document"]["id"] == str(document_id)
    assert dumped["document"]["ingested_at"] == "2026-04-19T00:00:00Z"
    assert dumped["chunk"]["id"] == str(chunk_id)
    assert dumped["chunk"]["created_at"] == "2026-04-19T00:05:00Z"
    assert dumped["chunk"]["metadata"] == {"section": "decision"}


def test_ingest_result_rejects_negative_counts() -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        IngestResult(
            documents_processed=1,
            chunks_created=2,
            documents_skipped=-1,
        )
