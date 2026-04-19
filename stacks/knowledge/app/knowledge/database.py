from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import psycopg
from pgvector.psycopg import register_vector
from psycopg import Connection, Cursor
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import Chunk, Document, SearchResult, normalize_embedding

DATABASE_URL_ENV = "KNOWLEDGE_DB_URL"

type DBRow = dict[str, Any]
type DatabaseConnection = Connection[Any]
type DatabaseCursor = Cursor[DBRow]


def resolve_database_url(db_url: str | None = None) -> str | None:
    """Resolve the database URL from explicit arg, env var, or libpq defaults.

    Returns None when no URL is configured — psycopg falls back to standard
    PG* environment variables (PGHOST, PGDATABASE, etc.) automatically.
    """
    if db_url is not None:
        return db_url

    return os.getenv(DATABASE_URL_ENV)


def connect(db_url: str | None = None) -> DatabaseConnection:
    url = resolve_database_url(db_url)
    connection = psycopg.connect(url) if url else psycopg.connect()
    register_vector(connection)
    return connection


def upsert_document(conn: DatabaseConnection, document: Document) -> Document:
    with _cursor(conn) as cursor:
        cursor.execute(
            """
            INSERT INTO documents (id, source_path, title, content_hash, ingested_at)
            VALUES (COALESCE(%s, gen_random_uuid()), %s, %s, %s, COALESCE(%s, now()))
            ON CONFLICT (source_path) DO UPDATE
            SET title = EXCLUDED.title,
                content_hash = EXCLUDED.content_hash,
                ingested_at = EXCLUDED.ingested_at
            RETURNING id, source_path, title, content_hash, ingested_at
            """,
            (
                document.id,
                document.source_path,
                document.title,
                document.content_hash,
                document.ingested_at,
            ),
        )
        return _document_from_row(_fetchone(cursor, operation="upsert document"))


def insert_chunks(conn: DatabaseConnection, chunks: list[Chunk]) -> list[Chunk]:
    if not chunks:
        return []

    sql = """
        INSERT INTO chunks (id, document_id, chunk_index, content, embedding, metadata, created_at)
        VALUES (COALESCE(%s, gen_random_uuid()), %s, %s, %s, %s, %s, COALESCE(%s, now()))
        RETURNING id, document_id, chunk_index, content, embedding, metadata, created_at
    """
    params_seq = [
        (
            c.id,
            c.document_id,
            c.chunk_index,
            c.content,
            c.embedding,
            Jsonb(c.metadata),
            c.created_at,
        )
        for c in chunks
    ]

    inserted: list[Chunk] = []
    with _cursor(conn) as cursor:
        cursor.executemany(sql, params_seq, returning=True)
        while True:
            row = cursor.fetchone()
            if row is None:
                if not cursor.nextset():
                    break
                continue
            inserted.append(_chunk_from_row(row))

    return inserted


def delete_document_chunks(conn: DatabaseConnection, document: Document) -> int:
    document_id = _require_document_id(document)
    with _cursor(conn) as cursor:
        cursor.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
        deleted_rows = cursor.rowcount

    return max(deleted_rows, 0)


def delete_document(conn: DatabaseConnection, document: Document) -> int:
    document_id = _require_document_id(document)
    with _cursor(conn) as cursor:
        cursor.execute("DELETE FROM documents WHERE id = %s", (document_id,))
        deleted_rows = cursor.rowcount

    return max(deleted_rows, 0)


def search_chunks(
    conn: DatabaseConnection,
    query_embedding: list[float],
    *,
    limit: int = 10,
) -> list[SearchResult]:
    if limit <= 0:
        raise ValueError("limit must be greater than zero")

    normalized_embedding = normalize_embedding(query_embedding)

    with _cursor(conn) as cursor:
        cursor.execute(
            """
            SELECT
                d.id AS document_id,
                d.source_path AS document_source_path,
                d.title AS document_title,
                d.content_hash AS document_content_hash,
                d.ingested_at AS document_ingested_at,
                c.id AS chunk_id,
                c.document_id AS chunk_document_id,
                c.chunk_index AS chunk_chunk_index,
                c.content AS chunk_content,
                c.metadata AS chunk_metadata,
                c.created_at AS chunk_created_at,
                GREATEST(0.0, LEAST(1.0, 1 - (c.embedding <=> %s::vector))) AS score
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            ORDER BY c.embedding <=> %s::vector
            LIMIT %s
            """,
            (normalized_embedding, normalized_embedding, limit),
        )
        rows = cursor.fetchall()

    return [_search_result_from_row(row) for row in rows]


def get_document_by_hash(
    conn: DatabaseConnection,
    content_hash: str,
) -> Document | None:
    normalized_hash = content_hash.strip()
    if not normalized_hash:
        raise ValueError("content_hash must not be blank")

    with _cursor(conn) as cursor:
        cursor.execute(
            """
            SELECT id, source_path, title, content_hash, ingested_at
            FROM documents
            WHERE content_hash = %s
            ORDER BY ingested_at DESC
            LIMIT 1
            """,
            (normalized_hash,),
        )
        row = cursor.fetchone()

    if row is None:
        return None

    return _document_from_row(row)


def get_document_by_source(
    conn: DatabaseConnection,
    source_path: str,
) -> Document | None:
    """Find a document by source_path."""
    normalized_path = source_path.strip()
    if not normalized_path:
        raise ValueError("source_path must not be blank")

    with _cursor(conn) as cursor:
        cursor.execute(
            """
            SELECT id, source_path, title, content_hash, ingested_at
            FROM documents
            WHERE source_path = %s
            LIMIT 1
            """,
            (normalized_path,),
        )
        row = cursor.fetchone()

    return _document_from_row(row) if row else None


def list_documents_by_source_prefix(
    conn: DatabaseConnection,
    source_prefix: str,
) -> list[Document]:
    normalized_prefix = source_prefix.strip()
    if not normalized_prefix:
        raise ValueError("source_prefix must not be blank")

    with _cursor(conn) as cursor:
        cursor.execute(
            """
            SELECT id, source_path, title, content_hash, ingested_at
            FROM documents
            WHERE source_path LIKE %s ESCAPE '\\'
            ORDER BY source_path
            """,
            (_like_prefix_pattern(normalized_prefix),),
        )
        rows = cursor.fetchall()

    return [_document_from_row(row) for row in rows]


def _like_prefix_pattern(prefix: str) -> str:
    escaped_prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped_prefix}%"


def _fetchone(cursor: DatabaseCursor, *, operation: str) -> DBRow:
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError(f"database did not return a row for {operation}")
    return row


def _cursor(conn: DatabaseConnection) -> DatabaseCursor:
    return Cursor(conn, row_factory=dict_row)


def _require_document_id(document: Document) -> UUID:
    if document.id is None:
        raise ValueError("document.id must be set before deleting chunks")
    return document.id


def _document_from_row(row: DBRow) -> Document:
    return Document.model_validate(row)


def _chunk_from_row(row: DBRow) -> Chunk:
    return Chunk.model_validate(row)


def _search_result_from_row(row: DBRow) -> SearchResult:
    return SearchResult(
        score=row["score"],
        document=Document.model_validate(
            {
                "id": row["document_id"],
                "source_path": row["document_source_path"],
                "title": row["document_title"],
                "content_hash": row["document_content_hash"],
                "ingested_at": row["document_ingested_at"],
            }
        ),
        chunk=Chunk.model_validate(
            {
                "id": row["chunk_id"],
                "document_id": row["chunk_document_id"],
                "chunk_index": row["chunk_chunk_index"],
                "content": row["chunk_content"],
                "metadata": row["chunk_metadata"],
                "created_at": row["chunk_created_at"],
            }
        ),
    )
