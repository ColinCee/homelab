"""End-to-end integration test for the knowledge ingest pipeline.

Runs against a real Postgres + pgvector instance. Skipped unless RUN_INTEGRATION_TESTS=1.
In CI a `services:` container provides Postgres; locally run with:

    docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=test \\
        pgvector/pgvector:0.8.2-pg17-trixie
    PGHOST=localhost PGUSER=postgres PGPASSWORD=test PGDATABASE=postgres \\
        RUN_INTEGRATION_TESTS=1 uv run pytest tests/integration/

Verifies the regression from issue #230: migrations are picked up from the
correct directory AND ingest+search round-trip succeeds with all stack pieces
wired together.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import LiteralString, cast
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="set RUN_INTEGRATION_TESTS=1 to run integration tests against real Postgres",
)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
INIT_SQL = Path(__file__).resolve().parents[3] / "init.sql"

EMBEDDING_DIMENSION = 3072


def _stub_embeddings(texts: list[str], *, token: str | None = None) -> list[list[float]]:
    # Deterministic non-zero unit vector — bypasses GitHub Models API.
    # Each call returns the same vector so the search query embedding matches
    # the stored chunk embedding exactly (cosine similarity = 1).
    vector = [0.0] * EMBEDDING_DIMENSION
    vector[0] = 1.0
    return [vector for _ in texts]


@pytest.fixture
def fresh_db() -> Iterator[None]:
    import psycopg

    from knowledge.database import connect, resolve_database_url, run_migrations

    # Bootstrap: run init.sql with a raw connection (before pgvector extension exists,
    # we can't use knowledge.database.connect which registers the vector type).
    raw = psycopg.connect(resolve_database_url() or "")
    try:
        with raw.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS chunks, note_links, documents CASCADE")
            cursor.execute(cast(LiteralString, INIT_SQL.read_text(encoding="utf-8")))
        raw.commit()
    finally:
        raw.close()

    conn = connect()
    try:
        run_migrations(conn)
        yield
    finally:
        conn.close()


def test_migrations_create_all_expected_tables(fresh_db: None) -> None:
    # Regression for bug 1: MIGRATIONS_DIR pointing at a missing directory
    # caused note_links to never be created. This test fails loudly if any
    # migration silently no-ops.
    from knowledge.database import _cursor, connect

    conn = connect()
    try:
        with _cursor(conn) as cursor:
            cursor.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
            )
            tables = [row["tablename"] for row in cursor.fetchall()]
    finally:
        conn.close()

    assert tables == ["chunks", "documents", "note_links"]


def test_ingest_then_search_roundtrip(fresh_db: None, tmp_path: Path) -> None:
    # Regression for the full bug class: with migrations applied and embeddings
    # stubbed, a freshly ingested note must be searchable.
    from knowledge.ingest import ingest_directory
    from knowledge.search import search

    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    fixture = FIXTURES_DIR / "sample_note.md"
    (notes_dir / "sample_note.md").write_text(fixture.read_text(encoding="utf-8"))

    with patch("knowledge.ingest.get_embeddings", side_effect=_stub_embeddings):
        result = ingest_directory(notes_dir, glob_pattern="**/*.md")

    assert result.documents_processed == 1
    assert result.files_failed == 0
    assert result.chunks_created >= 1

    with patch("knowledge.search.get_embeddings", side_effect=_stub_embeddings):
        hits = search("quokka habitat budget allocation 2026", limit=5)

    assert hits, "expected at least one search hit for the ingested fixture"
    assert any("sample_note.md" in hit.document.source_path for hit in hits)
