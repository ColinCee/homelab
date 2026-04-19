from unittest.mock import MagicMock, patch

import pytest

from knowledge.database import (
    DATABASE_URL_ENV,
    list_documents_by_source_prefix,
    resolve_database_url,
)


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
