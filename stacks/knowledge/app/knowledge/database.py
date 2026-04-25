from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, LiteralString, cast
from uuid import UUID

import psycopg
from pgvector.psycopg import register_vector
from psycopg import Connection, Cursor
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import Chunk, Document, NoteLink, RelatedDocument, SearchResult, normalize_embedding
from .tokenize import cjk_search_text, english_relaxed_query_text

DATABASE_URL_ENV = "KNOWLEDGE_DB_URL"


def _resolve_migrations_dir() -> Path:
    # Repo layout:      stacks/knowledge/app/knowledge/database.py + stacks/knowledge/migrations/
    # Container layout: /app/knowledge/database.py            + /app/migrations/
    here = Path(__file__).resolve()
    candidates = [here.parents[2] / "migrations", here.parents[1] / "migrations"]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        f"Could not locate migrations directory. Tried: {[str(c) for c in candidates]}"
    )


MIGRATIONS_DIR = _resolve_migrations_dir()

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


@contextmanager
def managed_connection(
    conn: DatabaseConnection | None = None,
) -> Generator[DatabaseConnection]:
    """Yield a database connection, closing it only if we created it."""
    own_conn = conn is None
    db = conn or connect()
    try:
        yield db
    finally:
        if own_conn:
            db.close()


def run_migrations(conn: DatabaseConnection) -> None:
    """Run schema migrations. Safe to call repeatedly (idempotent)."""
    with _cursor(conn) as cursor:
        for migration in _migration_files():
            cursor.execute(cast(LiteralString, migration.read_text(encoding="utf-8")))
        _backfill_cjk_tokens(cursor)
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
        INSERT INTO chunks (
            id, document_id, chunk_index, content, embedding, metadata, cjk_tokens, created_at
        )
        VALUES (COALESCE(%s, gen_random_uuid()), %s, %s, %s, %s, %s, %s, COALESCE(%s, now()))
        RETURNING id, document_id, chunk_index, content, embedding, metadata, cjk_tokens, created_at
    """
    params_seq = [
        (
            c.id,
            c.document_id,
            c.chunk_index,
            c.content,
            c.embedding,
            Jsonb(c.metadata),
            c.cjk_tokens or cjk_search_text(c.content),
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


def delete_note_links_for_source(
    conn: DatabaseConnection,
    document: Document,
    *,
    link_type: str | None = None,
) -> int:
    document_id = _require_document_id(document)
    sql = "DELETE FROM note_links WHERE source_id = %s"
    params: tuple[object, ...] = (document_id,)
    if link_type is not None:
        sql = f"{sql} AND link_type = %s"
        params = (document_id, link_type)

    with _cursor(conn) as cursor:
        cursor.execute(sql, params)
        deleted_rows = cursor.rowcount

    return max(deleted_rows, 0)


def delete_note_links(conn: DatabaseConnection, *, link_type: str | None = None) -> int:
    sql = "DELETE FROM note_links"
    params: tuple[object, ...] = ()
    if link_type is not None:
        sql = f"{sql} WHERE link_type = %s"
        params = (link_type,)

    with _cursor(conn) as cursor:
        cursor.execute(sql, params)
        deleted_rows = cursor.rowcount

    return max(deleted_rows, 0)


def insert_note_links(conn: DatabaseConnection, links: list[NoteLink]) -> int:
    if not links:
        return 0

    with _cursor(conn) as cursor:
        cursor.executemany(
            """
            INSERT INTO note_links (source_id, target_id, link_type, score)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (source_id, target_id, link_type) DO UPDATE
            SET score = EXCLUDED.score
            """,
            [(link.source_id, link.target_id, link.link_type, link.score) for link in links],
        )

    return len(links)


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
                c.metadata AS chunk_metadata,
                c.created_at AS chunk_created_at,
                GREATEST(0.0, LEAST(1.0, 1 - (c.embedding <=> %s::halfvec))) AS score
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            ORDER BY c.embedding <=> %s::halfvec
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
    """RRF hybrid: merge vector similarity and English/Chinese keyword rankings.

    Uses websearch_to_tsquery for safe parsing of natural-language queries.
    """
    candidate_limit = max(limit * 10, 50)
    cjk_query_text = cjk_search_text(query_text)
    english_relaxed_text = english_relaxed_query_text(query_text)

    with _cursor(conn) as cursor:
        cursor.execute(
            """
            WITH vector_ranked AS (
                SELECT c.id, ROW_NUMBER() OVER (ORDER BY c.embedding <=> %(emb)s::halfvec) AS rank_v
                FROM chunks c
                LIMIT %(candidates)s
            ),
            english_strict_query AS (
                SELECT websearch_to_tsquery('english', %(tsq)s) AS query
            ),
            english_strict_ranked AS (
                SELECT c.id,
                    ROW_NUMBER() OVER (
                        ORDER BY ts_rank_cd(c.tsv, q.query) DESC
                    ) AS rank_en_strict
                FROM chunks c
                CROSS JOIN english_strict_query q
                WHERE c.tsv @@ q.query
                LIMIT %(candidates)s
            ),
            english_relaxed_query AS (
                SELECT CASE
                    WHEN %(english_relaxed)s = '' THEN NULL::tsquery
                    ELSE websearch_to_tsquery('english', %(english_relaxed)s)
                END AS query
            ),
            english_relaxed_ranked AS (
                SELECT c.id,
                    ROW_NUMBER() OVER (
                        ORDER BY ts_rank_cd(c.tsv, q.query) DESC
                    ) AS rank_en_relaxed
                FROM chunks c
                CROSS JOIN english_relaxed_query q
                WHERE q.query IS NOT NULL AND c.tsv @@ q.query
                LIMIT %(candidates)s
            ),
            cjk_query AS (
                SELECT CASE
                    WHEN %(cjk_tsq)s = '' THEN NULL::tsquery
                    ELSE websearch_to_tsquery('simple', %(cjk_tsq)s)
                END AS query
            ),
            cjk_ranked AS (
                SELECT c.id,
                    ROW_NUMBER() OVER (
                        ORDER BY ts_rank_cd(c.tsv_zh, q.query) DESC
                    ) AS rank_cjk
                FROM chunks c
                CROSS JOIN cjk_query q
                WHERE q.query IS NOT NULL AND c.tsv_zh @@ q.query
                LIMIT %(candidates)s
            ),
            rrf_scores AS (
                SELECT v.id AS chunk_id, 1.0 / (%(k)s + v.rank_v) AS score
                FROM vector_ranked v
                UNION ALL
                SELECT s.id AS chunk_id, 1.0 / (%(k)s + s.rank_en_strict) AS score
                FROM english_strict_ranked s
                UNION ALL
                SELECT r.id AS chunk_id, 1.0 / (%(k)s + r.rank_en_relaxed) AS score
                FROM english_relaxed_ranked r
                UNION ALL
                SELECT z.id AS chunk_id, 1.0 / (%(k)s + z.rank_cjk) AS score
                FROM cjk_ranked z
            ),
            rrf AS (
                SELECT
                    chunk_id,
                    SUM(score) AS rrf_score
                FROM rrf_scores
                GROUP BY chunk_id
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
                "english_relaxed": english_relaxed_text,
                "cjk_tsq": cjk_query_text,
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


def list_documents(conn: DatabaseConnection) -> list[Document]:
    with _cursor(conn) as cursor:
        cursor.execute(
            """
            SELECT id, source_path, title, content_hash, ingested_at
            FROM documents
            ORDER BY source_path
            """
        )
        rows = cursor.fetchall()

    return [_document_from_row(row) for row in rows]


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


def find_similar_documents(
    conn: DatabaseConnection,
    document: Document,
    *,
    limit: int = 5,
) -> list[RelatedDocument]:
    document_id = _require_document_id(document)
    if limit <= 0:
        raise ValueError("limit must be greater than zero")

    with _cursor(conn) as cursor:
        cursor.execute(
            """
            WITH source_embedding AS (
                SELECT AVG(c.embedding) AS embedding
                FROM chunks c
                WHERE c.document_id = %(source_id)s
            ),
            candidate_embeddings AS (
                SELECT
                    d.id,
                    d.source_path,
                    d.title,
                    d.content_hash,
                    d.ingested_at,
                    AVG(c.embedding) AS embedding
                FROM documents d
                JOIN chunks c ON c.document_id = d.id
                WHERE d.id <> %(source_id)s
                GROUP BY d.id, d.source_path, d.title, d.content_hash, d.ingested_at
            )
            SELECT
                ce.id AS document_id,
                ce.source_path AS document_source_path,
                ce.title AS document_title,
                ce.content_hash AS document_content_hash,
                ce.ingested_at AS document_ingested_at,
                'similarity' AS link_type,
                GREATEST(0.0, LEAST(1.0, 1 - (ce.embedding <=> se.embedding))) AS score
            FROM candidate_embeddings ce
            JOIN source_embedding se ON se.embedding IS NOT NULL
            ORDER BY ce.embedding <=> se.embedding
            LIMIT %(limit)s
            """,
            {"source_id": document_id, "limit": limit},
        )
        rows = cursor.fetchall()

    return [_related_document_from_row(row) for row in rows]


def list_related_documents(
    conn: DatabaseConnection,
    document: Document,
) -> list[RelatedDocument]:
    document_id = _require_document_id(document)
    with _cursor(conn) as cursor:
        cursor.execute(
            """
            SELECT
                d.id AS document_id,
                d.source_path AS document_source_path,
                d.title AS document_title,
                d.content_hash AS document_content_hash,
                d.ingested_at AS document_ingested_at,
                nl.link_type,
                nl.score
            FROM note_links nl
            JOIN documents d ON d.id = nl.target_id
            WHERE nl.source_id = %s
            ORDER BY
                CASE nl.link_type WHEN 'wikilink' THEN 0 ELSE 1 END,
                nl.score DESC NULLS LAST,
                d.source_path
            """,
            (document_id,),
        )
        rows = cursor.fetchall()

    return [_related_document_from_row(row) for row in rows]


def _like_prefix_pattern(prefix: str) -> str:
    escaped_prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped_prefix}%"


def _backfill_cjk_tokens(cursor: DatabaseCursor) -> None:
    cursor.execute(
        """
        SELECT id, content
        FROM chunks
        WHERE cjk_tokens = '' AND content ~ '[一-龿]'
        """
    )
    rows = cursor.fetchall()
    if not rows:
        return

    cursor.executemany(
        "UPDATE chunks SET cjk_tokens = %s WHERE id = %s",
        [(cjk_search_text(row["content"]), row["id"]) for row in rows],
    )


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


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
                "cjk_tokens": row.get("chunk_cjk_tokens", ""),
                "created_at": row["chunk_created_at"],
            }
        ),
    )


def _related_document_from_row(row: DBRow) -> RelatedDocument:
    return RelatedDocument(
        link_type=row["link_type"],
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
    )
