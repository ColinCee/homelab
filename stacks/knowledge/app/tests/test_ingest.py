from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID

from knowledge.ingest import _title_from_file, ingest_file, ingest_text
from knowledge.models import EMBEDDING_DIMENSION, Document, IngestResult


def _fake_embeddings(texts: list[str], **_: object) -> list[list[float]]:
    return [[0.1] * EMBEDDING_DIMENSION for _ in texts]


def _fake_connect() -> MagicMock:
    conn = MagicMock()
    conn.commit = MagicMock()
    conn.close = MagicMock()
    return conn


@patch("knowledge.ingest.connect")
@patch("knowledge.ingest.get_embeddings", side_effect=_fake_embeddings)
@patch("knowledge.ingest.insert_chunks", return_value=[])
@patch("knowledge.ingest.upsert_document")
@patch("knowledge.ingest.delete_document_chunks")
@patch("knowledge.ingest.get_document_by_source", return_value=None)
def test_ingest_file_new_document(
    mock_get_source: MagicMock,
    mock_delete: MagicMock,
    mock_upsert: MagicMock,
    mock_insert: MagicMock,
    mock_embed: MagicMock,
    mock_connect: MagicMock,
    tmp_path: Path,
) -> None:
    # Arrange
    mock_connect.return_value = _fake_connect()
    saved_doc = Document(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        source_path="test.md",
        title="Test",
        content_hash="abc",
    )
    mock_upsert.return_value = saved_doc

    test_file = tmp_path / "test.md"
    test_file.write_text("# My Doc\n\nSome content here.")

    # Act
    result = ingest_file(test_file)

    # Assert
    assert isinstance(result, IngestResult)
    assert result.documents_processed == 1
    assert result.documents_skipped == 0
    mock_embed.assert_called_once()
    mock_delete.assert_not_called()


@patch("knowledge.ingest.connect")
@patch("knowledge.ingest.get_embeddings", side_effect=_fake_embeddings)
@patch("knowledge.ingest.insert_chunks", return_value=[])
@patch("knowledge.ingest.upsert_document")
@patch("knowledge.ingest.delete_document_chunks")
@patch("knowledge.ingest.get_document_by_source")
def test_ingest_skips_unchanged_content(
    mock_get_source: MagicMock,
    mock_delete: MagicMock,
    mock_upsert: MagicMock,
    mock_insert: MagicMock,
    mock_embed: MagicMock,
    mock_connect: MagicMock,
    tmp_path: Path,
) -> None:
    # Arrange
    content = "# Same Doc\n\nSame content."
    test_file = tmp_path / "same.md"
    test_file.write_text(content)

    import hashlib

    content_hash = hashlib.sha256(content.encode()).hexdigest()
    mock_connect.return_value = _fake_connect()
    mock_get_source.return_value = Document(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        source_path=str(test_file),
        title="Same Doc",
        content_hash=content_hash,
    )

    # Act
    result = ingest_file(test_file)

    # Assert
    assert result.documents_skipped == 1
    assert result.documents_processed == 0
    mock_embed.assert_not_called()
    mock_upsert.assert_not_called()


@patch("knowledge.ingest.connect")
@patch("knowledge.ingest.get_embeddings", side_effect=_fake_embeddings)
@patch("knowledge.ingest.insert_chunks", return_value=[])
@patch("knowledge.ingest.upsert_document")
@patch("knowledge.ingest.delete_document_chunks")
@patch("knowledge.ingest.get_document_by_source")
def test_ingest_reingests_changed_content(
    mock_get_source: MagicMock,
    mock_delete: MagicMock,
    mock_upsert: MagicMock,
    mock_insert: MagicMock,
    mock_embed: MagicMock,
    mock_connect: MagicMock,
    tmp_path: Path,
) -> None:
    # Arrange
    test_file = tmp_path / "changed.md"
    test_file.write_text("# Updated\n\nNew content.")

    existing = Document(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        source_path=str(test_file),
        title="Old",
        content_hash="old-hash",
    )
    mock_connect.return_value = _fake_connect()
    mock_get_source.return_value = existing
    mock_upsert.return_value = existing

    # Act
    result = ingest_file(test_file)

    # Assert
    assert result.documents_processed == 1
    mock_delete.assert_called_once()
    mock_embed.assert_called_once()


@patch("knowledge.ingest.connect")
@patch("knowledge.ingest.get_embeddings", side_effect=_fake_embeddings)
@patch("knowledge.ingest.insert_chunks", return_value=[])
@patch("knowledge.ingest.upsert_document")
@patch("knowledge.ingest.delete_document_chunks")
@patch("knowledge.ingest.get_document_by_source", return_value=None)
def test_ingest_text_uses_text_uri(
    mock_get_source: MagicMock,
    mock_delete: MagicMock,
    mock_upsert: MagicMock,
    mock_insert: MagicMock,
    mock_embed: MagicMock,
    mock_connect: MagicMock,
) -> None:
    # Arrange
    mock_connect.return_value = _fake_connect()
    saved = Document(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        source_path="text://Quick Note",
        title="Quick Note",
        content_hash="abc",
    )
    mock_upsert.return_value = saved

    # Act
    result = ingest_text("Some quick thought", title="Quick Note")

    # Assert
    assert result.documents_processed == 1
    call_args = mock_upsert.call_args[0]
    assert call_args[1].source_path.startswith("text://Quick Note/")


@patch("knowledge.ingest.connect")
@patch("knowledge.ingest.get_embeddings", side_effect=_fake_embeddings)
@patch("knowledge.ingest.insert_chunks", return_value=[])
@patch("knowledge.ingest.upsert_document")
@patch("knowledge.ingest.delete_document_chunks")
@patch("knowledge.ingest.get_document_by_source", return_value=None)
def test_ingest_text_different_content_same_title_no_collision(
    mock_get_source: MagicMock,
    mock_delete: MagicMock,
    mock_upsert: MagicMock,
    mock_insert: MagicMock,
    mock_embed: MagicMock,
    mock_connect: MagicMock,
) -> None:
    """Two text ingests with same title but different content get different source_paths."""
    # Arrange
    mock_connect.return_value = _fake_connect()
    saved = Document(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        source_path="text://Note/abc",
        title="Note",
        content_hash="abc",
    )
    mock_upsert.return_value = saved

    # Act
    ingest_text("Content A", title="Note")
    path_a = mock_upsert.call_args[0][1].source_path

    ingest_text("Content B", title="Note")
    path_b = mock_upsert.call_args[0][1].source_path

    # Assert — different content hashes produce different source paths
    assert path_a != path_b
    assert path_a.startswith("text://Note/")
    assert path_b.startswith("text://Note/")


@patch("knowledge.ingest.connect")
@patch("knowledge.ingest.get_embeddings", side_effect=_fake_embeddings)
@patch("knowledge.ingest.insert_chunks", return_value=[])
@patch("knowledge.ingest.upsert_document")
@patch("knowledge.ingest.delete_document_chunks")
@patch("knowledge.ingest.get_document_by_source")
def test_zero_chunks_on_reingest_deletes_stale_data(
    mock_get_source: MagicMock,
    mock_delete: MagicMock,
    mock_upsert: MagicMock,
    mock_insert: MagicMock,
    mock_embed: MagicMock,
    mock_connect: MagicMock,
    tmp_path: Path,
) -> None:
    """Re-ingesting a file that produces zero chunks should delete old chunks."""
    # Arrange — file with only a heading (chunker returns [])
    test_file = tmp_path / "empty.md"
    test_file.write_text("# Just A Heading")

    existing = Document(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        source_path=str(test_file),
        title="Old",
        content_hash="old-hash",
    )
    mock_connect.return_value = _fake_connect()
    mock_get_source.return_value = existing
    mock_upsert.return_value = existing

    # Act
    result = ingest_file(test_file)

    # Assert — stale chunks cleaned up, no embeddings requested
    assert result.documents_processed == 1
    assert result.chunks_created == 0
    mock_delete.assert_called_once()
    mock_embed.assert_not_called()


def test_title_from_markdown_heading(tmp_path: Path) -> None:
    content = "# My Great Title\n\nBody text."
    path = tmp_path / "doc.md"
    assert _title_from_file(path, content) == "My Great Title"


def test_title_from_filename(tmp_path: Path) -> None:
    path = tmp_path / "my-cool-doc.txt"
    assert _title_from_file(path, "plain text") == "My Cool Doc"
