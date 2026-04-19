from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import psycopg
from pgvector.psycopg import register_vector
from psycopg import Connection, Cursor
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import Chunk, Document, SearchResult, Workspace, normalize_embedding

DATABASE_URL_ENV = "KNOWLEDGE_DB_URL"

type DBRow = dict[str, Any]
type DatabaseConnection = Connection[Any]
type DatabaseCursor = Cursor[DBRow]


def resolve_database_url(db_url: str | None = None) -> str:
    if db_url is not None:
        return db_url

    env_db_url = os.getenv(DATABASE_URL_ENV)
    if env_db_url:
        return env_db_url

    raise RuntimeError(f"{DATABASE_URL_ENV} must be set to a PostgreSQL DSN")


def connect(db_url: str | None = None) -> DatabaseConnection:
    connection = psycopg.connect(resolve_database_url(db_url))
    register_vector(connection)
    return connection


def create_workspace(conn: DatabaseConnection, workspace: Workspace) -> Workspace:
    with _cursor(conn) as cursor:
        cursor.execute(
            """
            INSERT INTO workspaces (name, description)
            VALUES (%s, %s)
            ON CONFLICT (name) DO UPDATE
            SET description = EXCLUDED.description
            RETURNING name, description, created_at
            """,
            (workspace.name, workspace.description),
        )
        return _workspace_from_row(_fetchone(cursor, operation="create workspace"))


def upsert_document(conn: DatabaseConnection, document: Document) -> Document:
    with _cursor(conn) as cursor:
        cursor.execute(
            """
            INSERT INTO documents (id, workspace, source_path, title, content_hash, ingested_at)
            VALUES (COALESCE(%s, gen_random_uuid()), %s, %s, %s, %s, COALESCE(%s, now()))
            ON CONFLICT (workspace, source_path) DO UPDATE
            SET title = EXCLUDED.title,
                content_hash = EXCLUDED.content_hash,
                ingested_at = EXCLUDED.ingested_at
            RETURNING id, workspace, source_path, title, content_hash, ingested_at
            """,
            (
                document.id,
                document.workspace,
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
    params = [
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
        for row_params in params:
            cursor.execute(sql, row_params)
            inserted.append(_chunk_from_row(_fetchone(cursor, operation="insert chunk")))

    return inserted


def delete_document_chunks(conn: DatabaseConnection, document: Document) -> int:
    document_id = _require_document_id(document)
    with _cursor(conn) as cursor:
        cursor.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
        deleted_rows = cursor.rowcount

    return max(deleted_rows, 0)


def search_chunks(
    conn: DatabaseConnection,
    query_embedding: list[float],
    *,
    workspace: str | None = None,
    limit: int = 10,
) -> list[SearchResult]:
    if limit <= 0:
        raise ValueError("limit must be greater than zero")

    normalized_embedding = normalize_embedding(query_embedding)
    normalized_workspace = workspace.strip() if workspace is not None else None
    if normalized_workspace == "":
        raise ValueError("workspace must not be blank")

    with _cursor(conn) as cursor:
        cursor.execute(
            """
            WITH ranked_chunks AS (
                SELECT
                    w.name AS workspace_name,
                    d.id AS document_id,
                    d.workspace AS document_workspace,
                    d.source_path AS document_source_path,
                    d.title AS document_title,
                    d.content_hash AS document_content_hash,
                    d.ingested_at AS document_ingested_at,
                    c.id AS chunk_id,
                    c.document_id AS chunk_document_id,
                    c.chunk_index AS chunk_chunk_index,
                    c.content AS chunk_content,
                    c.embedding AS chunk_embedding,
                    c.metadata AS chunk_metadata,
                    c.created_at AS chunk_created_at,
                    c.embedding <=> %s AS distance
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                JOIN workspaces w ON w.name = d.workspace
                WHERE (%s IS NULL OR w.name = %s)
                ORDER BY distance
                LIMIT %s
            )
            SELECT
                workspace_name,
                document_id,
                document_workspace,
                document_source_path,
                document_title,
                document_content_hash,
                document_ingested_at,
                chunk_id,
                chunk_document_id,
                chunk_chunk_index,
                chunk_content,
                chunk_embedding,
                chunk_metadata,
                chunk_created_at,
                GREATEST(0.0, LEAST(1.0, 1 - distance)) AS score
            FROM ranked_chunks
            ORDER BY distance
            """,
            (normalized_embedding, normalized_workspace, normalized_workspace, limit),
        )
        rows = cursor.fetchall()

    return [_search_result_from_row(row) for row in rows]


def list_workspaces(conn: DatabaseConnection) -> list[Workspace]:
    with _cursor(conn) as cursor:
        cursor.execute(
            """
            SELECT name, description, created_at
            FROM workspaces
            ORDER BY name
            """
        )
        rows = cursor.fetchall()

    return [_workspace_from_row(row) for row in rows]


def get_document_by_hash(
    conn: DatabaseConnection,
    workspace: str,
    content_hash: str,
) -> Document | None:
    normalized_workspace = workspace.strip()
    normalized_hash = content_hash.strip()
    if not normalized_workspace:
        raise ValueError("workspace must not be blank")
    if not normalized_hash:
        raise ValueError("content_hash must not be blank")

    with _cursor(conn) as cursor:
        cursor.execute(
            """
            SELECT id, workspace, source_path, title, content_hash, ingested_at
            FROM documents
            WHERE workspace = %s AND content_hash = %s
            ORDER BY ingested_at DESC
            LIMIT 1
            """,
            (normalized_workspace, normalized_hash),
        )
        row = cursor.fetchone()

    if row is None:
        return None

    return _document_from_row(row)


def get_document_by_source(
    conn: DatabaseConnection,
    workspace: str,
    source_path: str,
) -> Document | None:
    """Find a document by workspace + source_path."""
    normalized_workspace = workspace.strip()
    normalized_path = source_path.strip()
    if not normalized_workspace:
        raise ValueError("workspace must not be blank")
    if not normalized_path:
        raise ValueError("source_path must not be blank")

    with _cursor(conn) as cursor:
        cursor.execute(
            """
            SELECT id, workspace, source_path, title, content_hash, ingested_at
            FROM documents
            WHERE workspace = %s AND source_path = %s
            LIMIT 1
            """,
            (normalized_workspace, normalized_path),
        )
        row = cursor.fetchone()

    return _document_from_row(row) if row else None


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


def _workspace_from_row(row: DBRow) -> Workspace:
    return Workspace.model_validate(row)


def _document_from_row(row: DBRow) -> Document:
    return Document.model_validate(row)


def _chunk_from_row(row: DBRow) -> Chunk:
    return Chunk.model_validate(row)


def _search_result_from_row(row: DBRow) -> SearchResult:
    return SearchResult(
        score=row["score"],
        workspace=row["workspace_name"],
        document=Document.model_validate(
            {
                "id": row["document_id"],
                "workspace": row["document_workspace"],
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
                "embedding": row["chunk_embedding"],
                "metadata": row["chunk_metadata"],
                "created_at": row["chunk_created_at"],
            }
        ),
    )
