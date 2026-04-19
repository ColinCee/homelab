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


def run_migrations(conn: DatabaseConnection) -> None:
    """Run schema migrations. Safe to call repeatedly (idempotent)."""
    with _cursor(conn) as cursor:
        cursor.execute("""
            ALTER TABLE chunks
                ADD COLUMN IF NOT EXISTS tsv tsvector
                GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv)")
    conn.commit()


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
    query_text: str = "",
) -> list[SearchResult]:
    if limit <= 0:
        raise ValueError("limit must be greater than zero")

    normalized_embedding = normalize_embedding(query_embedding)

    if query_text.strip():
        return _hybrid_search(conn, normalized_embedding, query_text, limit)
    return _vector_search(conn, normalized_embedding, limit)


def _vector_search(
    conn: DatabaseConnection,
    embedding: list[float],
    limit: int,
) -> list[SearchResult]:
    """Pure vector similarity search."""
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
                c.embedding AS chunk_embedding,
                c.metadata AS chunk_metadata,
                c.created_at AS chunk_created_at,
                GREATEST(0.0, LEAST(1.0, 1 - (c.embedding <=> %s::vector))) AS score
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            ORDER BY c.embedding <=> %s::vector
            LIMIT %s
            """,
            (embedding, embedding, limit),
        )
        rows = cursor.fetchall()

    return [_search_result_from_row(row) for row in rows]


_RRF_K = 60


def _hybrid_search(
    conn: DatabaseConnection,
    embedding: list[float],
    query_text: str,
    limit: int,
) -> list[SearchResult]:
    """RRF hybrid: merge vector similarity and keyword (tsvector) rankings.

    Uses websearch_to_tsquery for safe parsing of natural-language queries.
    """
    candidate_limit = limit * 3

    with _cursor(conn) as cursor:
        cursor.execute(
            """
            WITH vector_ranked AS (
                SELECT c.id, ROW_NUMBER() OVER (ORDER BY c.embedding <=> %(emb)s::vector) AS rank_v
                FROM chunks c
                LIMIT %(candidates)s
            ),
            keyword_ranked AS (
                SELECT c.id,
                    ROW_NUMBER() OVER (
                        ORDER BY ts_rank_cd(c.tsv, websearch_to_tsquery('english', %(tsq)s)) DESC
                    ) AS rank_k
                FROM chunks c
                WHERE c.tsv @@ websearch_to_tsquery('english', %(tsq)s)
                LIMIT %(candidates)s
            ),
            rrf AS (
                SELECT
                    COALESCE(v.id, k.id) AS chunk_id,
                    COALESCE(1.0 / (%(k)s + v.rank_v), 0.0)
                      + COALESCE(1.0 / (%(k)s + k.rank_k), 0.0) AS rrf_score
                FROM vector_ranked v
                FULL OUTER JOIN keyword_ranked k ON v.id = k.id
                ORDER BY rrf_score DESC
                LIMIT %(lim)s
            )
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
                c.embedding AS chunk_embedding,
                c.metadata AS chunk_metadata,
                c.created_at AS chunk_created_at,
                rrf.rrf_score AS score
            FROM rrf
            JOIN chunks c ON c.id = rrf.chunk_id
            JOIN documents d ON d.id = c.document_id
            ORDER BY rrf.rrf_score DESC
            """,
            {
                "emb": embedding,
                "tsq": query_text,
                "k": _RRF_K,
                "candidates": candidate_limit,
                "lim": limit,
            },
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
                "embedding": row["chunk_embedding"],
                "metadata": row["chunk_metadata"],
                "created_at": row["chunk_created_at"],
            }
        ),
    )
