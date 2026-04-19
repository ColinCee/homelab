import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from knowledge import __main__ as cli
from knowledge.ingest import (
    _extract_pdf_text,
    _file_content_hash,
    _read_file_content,
    _title_from_file,
    ingest_directory,
    ingest_file,
    ingest_text,
)
from knowledge.models import EMBEDDING_DIMENSION, DirectoryIngestResult, Document, IngestResult


def _fake_embeddings(texts: list[str], **_: object) -> list[list[float]]:
    return [[0.1] * EMBEDDING_DIMENSION for _ in texts]


def _fake_connect() -> MagicMock:
    conn = MagicMock()
    conn.commit = MagicMock()
    conn.close = MagicMock()
    conn.rollback = MagicMock()
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


@patch("knowledge.ingest.connect")
@patch("knowledge.ingest.delete_document", return_value=1)
@patch("knowledge.ingest.list_documents_by_source_prefix")
@patch("knowledge.ingest.ingest_file")
def test_ingest_directory_reuses_connection_and_deletes_orphans(
    mock_ingest_file: MagicMock,
    mock_list_prefix: MagicMock,
    mock_delete_document: MagicMock,
    mock_connect: MagicMock,
    tmp_path: Path,
) -> None:
    # Arrange
    notes_dir = tmp_path / "notes"
    nested_dir = notes_dir / "nested"
    nested_dir.mkdir(parents=True)
    first_file = notes_dir / "alpha.md"
    second_file = nested_dir / "beta.txt"
    ignored_file = notes_dir / "gamma.json"
    first_file.write_text("# Alpha\n\nBody")
    second_file.write_text("Plain text note")
    ignored_file.write_text("{}")

    orphan = Document(
        id=UUID("00000000-0000-0000-0000-000000000099"),
        source_path=str((notes_dir / "missing.md").resolve()),
        title="Missing",
        content_hash="orphan-hash",
    )
    conn = _fake_connect()
    mock_connect.return_value = conn
    mock_list_prefix.return_value = [orphan]
    mock_ingest_file.side_effect = [
        IngestResult(documents_processed=1, chunks_created=2, documents_skipped=0),
        IngestResult(documents_processed=0, chunks_created=0, documents_skipped=1),
    ]

    # Act
    result = ingest_directory(notes_dir)

    # Assert
    assert result == DirectoryIngestResult(
        files_found=2,
        files_failed=0,
        documents_processed=1,
        chunks_created=2,
        documents_skipped=1,
        documents_deleted=1,
    )
    mock_connect.assert_called_once()
    assert [call.args[0] for call in mock_ingest_file.call_args_list] == [
        first_file.resolve(),
        second_file.resolve(),
    ]
    assert all(call.kwargs["conn"] is conn for call in mock_ingest_file.call_args_list)
    mock_list_prefix.assert_called_once_with(conn, f"{notes_dir.resolve()}/")
    mock_delete_document.assert_called_once_with(conn, orphan)
    conn.close.assert_called_once()


@patch("knowledge.ingest.connect")
@patch("knowledge.ingest.delete_document", return_value=0)
@patch("knowledge.ingest.list_documents_by_source_prefix", return_value=[])
@patch("knowledge.ingest.ingest_file")
def test_ingest_directory_continues_after_file_error(
    mock_ingest_file: MagicMock,
    mock_list_prefix: MagicMock,
    mock_delete_document: MagicMock,
    mock_connect: MagicMock,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    broken_file = notes_dir / "broken.md"
    good_file = notes_dir / "good.md"
    broken_file.write_text("# Broken\n\nBody")
    good_file.write_text("# Good\n\nBody")

    conn = _fake_connect()
    mock_connect.return_value = conn
    mock_ingest_file.side_effect = [
        RuntimeError("boom"),
        IngestResult(documents_processed=1, chunks_created=3, documents_skipped=0),
    ]

    # Act
    with caplog.at_level(logging.ERROR):
        result = ingest_directory(notes_dir)

    # Assert
    assert result == DirectoryIngestResult(
        files_found=2,
        files_failed=1,
        documents_processed=1,
        chunks_created=3,
        documents_skipped=0,
        documents_deleted=0,
    )
    conn.rollback.assert_called_once()
    assert "Failed to ingest" in caplog.text
    mock_delete_document.assert_not_called()
    mock_list_prefix.assert_called_once()


@patch("knowledge.ingest.connect")
@patch("knowledge.ingest.delete_document", return_value=1)
@patch("knowledge.ingest.list_documents_by_source_prefix")
@patch("knowledge.ingest.ingest_file")
def test_ingest_directory_applies_custom_glob(
    mock_ingest_file: MagicMock,
    mock_list_prefix: MagicMock,
    mock_delete_document: MagicMock,
    mock_connect: MagicMock,
    tmp_path: Path,
) -> None:
    # Arrange
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    markdown_file = notes_dir / "alpha.md"
    text_file = notes_dir / "beta.txt"
    markdown_file.write_text("# Alpha\n\nBody")
    text_file.write_text("Body")
    existing_markdown = Document(
        id=UUID("00000000-0000-0000-0000-000000000010"),
        source_path=str(markdown_file.resolve()),
        title="Alpha",
        content_hash="hash-alpha",
    )
    missing_markdown = Document(
        id=UUID("00000000-0000-0000-0000-000000000011"),
        source_path=str((notes_dir / "missing.md").resolve()),
        title="Missing",
        content_hash="hash-missing",
    )

    conn = _fake_connect()
    mock_connect.return_value = conn
    mock_list_prefix.return_value = [existing_markdown, missing_markdown]
    mock_ingest_file.return_value = IngestResult(
        documents_processed=1,
        chunks_created=1,
        documents_skipped=0,
    )

    # Act
    result = ingest_directory(notes_dir, glob_pattern="**/*.txt")

    # Assert
    assert result.files_found == 1
    assert result.documents_deleted == 1
    mock_ingest_file.assert_called_once_with(text_file.resolve(), conn=conn, token=None)
    mock_delete_document.assert_called_once_with(conn, missing_markdown)
    mock_list_prefix.assert_called_once()


def test_cli_ingest_directory_prints_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # Arrange
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    expected = DirectoryIngestResult(
        files_found=2,
        files_failed=0,
        documents_processed=1,
        chunks_created=4,
        documents_skipped=1,
        documents_deleted=1,
    )
    calls: dict[str, object] = {}

    def fake_ingest_directory(
        directory: Path, *, glob_pattern: str, token: str | None = None
    ) -> DirectoryIngestResult:
        calls["directory"] = directory
        calls["glob_pattern"] = glob_pattern
        calls["token"] = token
        return expected

    monkeypatch.setattr(cli, "connect", lambda: MagicMock())
    monkeypatch.setattr(cli, "run_migrations", lambda conn: None)
    monkeypatch.setattr(cli, "ingest_directory", fake_ingest_directory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "knowledge",
            "ingest",
            "--dir",
            str(notes_dir),
            "--glob",
            "**/*.txt",
        ],
    )

    # Act
    cli.main()

    # Assert
    assert calls == {
        "directory": notes_dir,
        "glob_pattern": "**/*.txt",
        "token": None,
    }
    assert json.loads(capsys.readouterr().out) == expected.model_dump(mode="json")


def test_cli_rejects_glob_without_directory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # Arrange
    file_path = tmp_path / "note.md"
    file_path.write_text("# Note\n\nBody")
    monkeypatch.setattr(cli, "connect", lambda: MagicMock())
    monkeypatch.setattr(cli, "run_migrations", lambda conn: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "knowledge",
            "ingest",
            "--path",
            str(file_path),
            "--glob",
            "**/*.txt",
        ],
    )

    # Act / Assert
    with pytest.raises(SystemExit, match="1"):
        cli.main()

    assert capsys.readouterr().err.strip() == "Error: --glob requires --dir"


def test_cli_rejects_multiple_ingest_sources(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # Arrange
    notes_dir = tmp_path / "notes"
    file_path = tmp_path / "note.md"
    notes_dir.mkdir()
    file_path.write_text("# Note\n\nBody")
    monkeypatch.setattr(cli, "connect", lambda: MagicMock())
    monkeypatch.setattr(cli, "run_migrations", lambda conn: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "knowledge",
            "ingest",
            "--path",
            str(file_path),
            "--dir",
            str(notes_dir),
        ],
    )

    # Act / Assert
    with pytest.raises(SystemExit, match="1"):
        cli.main()

    assert (
        capsys.readouterr().err.strip() == "Error: provide exactly one of --path, --dir, or --text"
    )


def test_title_from_markdown_heading(tmp_path: Path) -> None:
    content = "# My Great Title\n\nBody text."
    path = tmp_path / "doc.md"
    assert _title_from_file(path, content) == "My Great Title"


def test_title_from_filename(tmp_path: Path) -> None:
    path = tmp_path / "my-cool-doc.txt"
    assert _title_from_file(path, "plain text") == "My Cool Doc"


def _create_test_pdf(path: Path, text: str = "Hello from PDF") -> None:
    """Create a minimal PDF with text content for testing."""
    content_stream = f"BT /F1 12 Tf 100 700 Td ({text}) Tj ET"
    pdf_bytes = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        b"4 0 obj<</Length " + str(len(content_stream)).encode() + b">>\n"
        b"stream\n" + content_stream.encode() + b"\nendstream\nendobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"xref\n0 6\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000266 00000 n \n"
        b"0000000000 00000 n \n"
        b"trailer<</Size 6/Root 1 0 R>>\n"
        b"startxref\n0\n%%EOF"
    )
    path.write_bytes(pdf_bytes)


def test_extract_pdf_text(tmp_path: Path) -> None:
    """PDF text extraction returns page content."""
    pdf_path = tmp_path / "test.pdf"
    _create_test_pdf(pdf_path, "Sample document text")
    result = _extract_pdf_text(pdf_path)
    assert "Sample document text" in result


def test_read_file_content_pdf(tmp_path: Path) -> None:
    """_read_file_content dispatches to PDF extraction for .pdf files."""
    pdf_path = tmp_path / "test.pdf"
    _create_test_pdf(pdf_path, "PDF content here")
    result = _read_file_content(pdf_path)
    assert "PDF content here" in result


def test_read_file_content_markdown(tmp_path: Path) -> None:
    """_read_file_content reads text files normally."""
    md_path = tmp_path / "test.md"
    md_path.write_text("# Hello\n\nWorld")
    result = _read_file_content(md_path)
    assert result == "# Hello\n\nWorld"


@patch("knowledge.ingest.connect")
@patch("knowledge.ingest.get_embeddings", side_effect=_fake_embeddings)
@patch("knowledge.ingest.insert_chunks", return_value=[])
@patch("knowledge.ingest.upsert_document")
@patch("knowledge.ingest.delete_document_chunks")
@patch("knowledge.ingest.get_document_by_source", return_value=None)
def test_ingest_pdf_file(
    mock_get_source: MagicMock,
    mock_delete: MagicMock,
    mock_upsert: MagicMock,
    mock_insert: MagicMock,
    mock_embed: MagicMock,
    mock_connect: MagicMock,
    tmp_path: Path,
) -> None:
    """Full ingest_file pipeline works for PDFs."""
    mock_connect.return_value = _fake_connect()
    saved_doc = Document(
        id=UUID("00000000-0000-0000-0000-000000000002"),
        source_path="test.pdf",
        title="Test",
        content_hash="abc",
    )
    mock_upsert.return_value = saved_doc

    pdf_path = tmp_path / "test.pdf"
    _create_test_pdf(pdf_path, "Ingestible PDF content")

    result = ingest_file(pdf_path)

    assert isinstance(result, IngestResult)
    assert result.documents_processed == 1
    mock_embed.assert_called_once()


def test_title_from_pdf_uses_filename(tmp_path: Path) -> None:
    """PDF titles use filename since there's no markdown heading."""
    path = tmp_path / "uk261-regulation.pdf"
    assert _title_from_file(path, "some extracted text") == "Uk261 Regulation"


def test_pdf_hash_uses_raw_bytes(tmp_path: Path) -> None:
    """PDF change detection hashes raw bytes, not extracted text.

    A PDF with different bytes but identical extracted text must produce
    a different hash so re-ingestion is triggered.
    """
    pdf_a = tmp_path / "a.pdf"
    pdf_b = tmp_path / "b.pdf"
    _create_test_pdf(pdf_a, "Same text")
    _create_test_pdf(pdf_b, "Same text")

    # Append garbage bytes to pdf_b — extracted text is unchanged
    with pdf_b.open("ab") as f:
        f.write(b"\x00" * 64)

    assert _extract_pdf_text(pdf_a) == _extract_pdf_text(pdf_b)
    assert _file_content_hash(pdf_a) != _file_content_hash(pdf_b)


@patch("knowledge.ingest.connect")
@patch("knowledge.ingest.get_embeddings", side_effect=RuntimeError("API down"))
@patch("knowledge.ingest.get_document_by_source", return_value=None)
def test_ingest_file_rolls_back_shared_connection_on_error(
    mock_get_source: MagicMock,
    mock_embed: MagicMock,
    mock_connect: MagicMock,
    tmp_path: Path,
) -> None:
    """When a shared connection is passed and ingest fails, it must be rolled back."""
    # Arrange
    md_file = tmp_path / "note.md"
    md_file.write_text("# Note\n\nSome content for embedding.")
    shared_conn = _fake_connect()

    # Act
    with pytest.raises(RuntimeError, match="API down"):
        ingest_file(md_file, conn=shared_conn)

    # Assert — shared connection rolled back, NOT closed (caller owns it)
    shared_conn.rollback.assert_called_once()
    shared_conn.close.assert_not_called()
