from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from knowledge.database import (
    DATABASE_URL_ENV,
    MIGRATIONS_DIR,
    _backfill_cjk_tokens,
    _migration_files,
    delete_note_links,
    insert_chunks,
    list_documents_by_source_prefix,
    list_related_documents,
    resolve_database_url,
    run_migrations,
    search_chunks,
)
from knowledge.models import EMBEDDING_DIMENSION, Chunk, Document


def test_resolve_database_url_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    database_url = "postgresql://knowledge:secret@100.100.146.119:5432/knowledge"
    monkeypatch.setenv(DATABASE_URL_ENV, database_url)

    # Act
    resolved = resolve_database_url()

    # Assert
    assert resolved == database_url


def test_resolve_database_url_falls_back_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange — no KNOWLEDGE_DB_URL set
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)

    # Act
    result = resolve_database_url()

    # Assert — None signals psycopg to use PG* env vars
    assert result is None


def test_migrations_dir_resolves_to_real_directory_with_sql_files() -> None:
    # Regression: MIGRATIONS_DIR previously resolved to a path that didn't exist
    # in the container, causing run_migrations() to silently no-op.
    # Assert
    assert MIGRATIONS_DIR.is_dir(), f"MIGRATIONS_DIR does not exist: {MIGRATIONS_DIR}"
    sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    assert sql_files, f"no .sql files found under {MIGRATIONS_DIR}"
    assert _migration_files() == sql_files


def test_migration_files_returns_sorted_sql_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Arrange
    later = tmp_path / "010_later.sql"
    first = tmp_path / "002_first.sql"
    later.write_text("SELECT 10;")
    first.write_text("SELECT 2;")
    monkeypatch.setattr("knowledge.database.MIGRATIONS_DIR", tmp_path)

    # Act
    migration_files = _migration_files()

    # Assert
    assert migration_files == [first, later]


@patch("knowledge.database._migration_files")
@patch("knowledge.database._cursor")
def test_run_migrations_executes_all_sql_files(
    mock_cursor_factory: MagicMock,
    mock_migration_files: MagicMock,
    tmp_path: Path,
) -> None:
    # Arrange
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    mock_cursor_factory.return_value.__enter__.return_value = cursor
    first = tmp_path / "001_first.sql"
    second = tmp_path / "002_second.sql"
    first.write_text("SELECT 1;")
    second.write_text("SELECT 2;")
    mock_migration_files.return_value = [first, second]

    # Act
    run_migrations(conn)

    # Assert
    assert cursor.execute.call_args_list[0].args == ("SELECT 1;",)
    assert cursor.execute.call_args_list[1].args == ("SELECT 2;",)
    assert "WHERE cjk_tokens = ''" in cursor.execute.call_args_list[2].args[0]
    conn.commit.assert_called_once_with()


def test_backfill_cjk_tokens_updates_existing_chinese_chunks() -> None:
    # Arrange
    cursor = MagicMock()
    chunk_id = UUID("00000000-0000-0000-0000-000000000001")
    cursor.fetchall.return_value = [{"id": chunk_id, "content": "内疚狼狈堕落胆怯"}]

    # Act
    _backfill_cjk_tokens(cursor)

    # Assert
    cursor.executemany.assert_called_once_with(
        "UPDATE chunks SET cjk_tokens = %s WHERE id = %s",
        [("内疚 狼狈 堕落 胆怯", chunk_id)],
    )


@patch("knowledge.database._cursor")
def test_insert_chunks_derives_cjk_tokens(
    mock_cursor_factory: MagicMock,
) -> None:
    # Arrange
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    cursor.nextset.return_value = False
    mock_cursor_factory.return_value.__enter__.return_value = cursor
    chunk = Chunk(
        document_id=UUID("00000000-0000-0000-0000-000000000001"),
        chunk_index=0,
        content="憧憬蹒跚徜徉褴褛聆听",
        embedding=[0.1] * EMBEDDING_DIMENSION,
    )

    # Act
    inserted = insert_chunks(conn, [chunk])

    # Assert
    assert inserted == []
    params = cursor.executemany.call_args.args[1]
    assert params[0][6] == "憧憬 蹒跚 徜徉 褴褛 聆听"


@patch("knowledge.database._cursor")
def test_search_chunks_fuses_vector_english_and_chinese_rankers(
    mock_cursor_factory: MagicMock,
) -> None:
    # Arrange
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    mock_cursor_factory.return_value.__enter__.return_value = cursor
    embedding = [0.1] * EMBEDDING_DIMENSION

    # Act
    results = search_chunks(conn, embedding, limit=5, query_text="内疚狼狈堕落胆怯")

    # Assert
    assert results == []
    sql, params = cursor.execute.call_args.args
    assert "english_strict_ranked" in sql
    assert "english_relaxed_ranked" in sql
    assert "cjk_ranked" in sql
    assert "UNION ALL" in sql
    assert params["candidates"] == 50
    assert params["cjk_tsq"] == "内疚 狼狈 堕落 胆怯"


@patch("knowledge.database._cursor")
def test_list_documents_by_source_prefix_escapes_like_metacharacters(
    mock_cursor_factory: MagicMock,
) -> None:
    # Arrange
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    mock_cursor_factory.return_value.__enter__.return_value = cursor

    # Act
    result = list_documents_by_source_prefix(conn, r"/tmp/notes_100%\archive")

    # Assert
    assert result == []
    sql, params = cursor.execute.call_args.args
    assert "LIKE %s ESCAPE '\\'" in sql
    assert params == (r"/tmp/notes\_100\%\\archive%",)


@patch("knowledge.database._cursor")
def test_delete_note_links_filters_by_link_type(
    mock_cursor_factory: MagicMock,
) -> None:
    # Arrange
    conn = MagicMock()
    cursor = MagicMock()
    cursor.rowcount = 3
    mock_cursor_factory.return_value.__enter__.return_value = cursor

    # Act
    deleted = delete_note_links(conn, link_type="similarity")

    # Assert
    sql, params = cursor.execute.call_args.args
    assert sql == "DELETE FROM note_links WHERE link_type = %s"
    assert params == ("similarity",)
    assert deleted == 3


@patch("knowledge.database._cursor")
def test_list_related_documents_queries_note_links(
    mock_cursor_factory: MagicMock,
) -> None:
    # Arrange
    conn = MagicMock()
    cursor = MagicMock()
    mock_cursor_factory.return_value.__enter__.return_value = cursor
    source = Document(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        source_path="docs/source.md",
        title="Source",
        content_hash="hash-source",
    )
    cursor.fetchall.return_value = [
        {
            "document_id": UUID("00000000-0000-0000-0000-000000000002"),
            "document_source_path": "docs/linked.md",
            "document_title": "Linked",
            "document_content_hash": "hash-linked",
            "document_ingested_at": None,
            "link_type": "wikilink",
            "score": None,
        }
    ]

    # Act
    results = list_related_documents(conn, source)

    # Assert
    sql, params = cursor.execute.call_args.args
    assert "FROM note_links nl" in sql
    assert "CASE nl.link_type WHEN 'wikilink' THEN 0 ELSE 1 END" in sql
    assert params == (source.id,)
    assert results[0].link_type == "wikilink"
    assert results[0].document.source_path == "docs/linked.md"
