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

import json
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


def _eval_embeddings(texts: list[str], *, token: str | None = None) -> list[list[float]]:
    # Make vector retrieval point at the background note by default. The eval
    # then needs lexical RRF signals to lift non-background expected results.
    return [_unit_vector(_eval_embedding_index(text)) for text in texts]


def _eval_embedding_index(text: str) -> int:
    if "Language Background" in text or "Cantonese Mandarin tone interference" in text:
        return 0
    if "Mandarin Learning Strategy" in text:
        return 1
    if "Anki Retention" in text:
        return 2
    if "Song-Based Character" in text:
        return 3
    if "Song Vocabulary" in text:
        return 4
    return 0


def _unit_vector(index: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSION
    vector[index] = 1.0
    return vector


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


def test_hnsw_index_exists_on_embedding_column(fresh_db: None) -> None:
    # The HNSW index is the whole point of the halfvec migration — verify it
    # exists so a future schema change doesn't silently regress to seq scans.
    from knowledge.database import _cursor, connect

    conn = connect()
    try:
        with _cursor(conn) as cursor:
            cursor.execute(
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE tablename = 'chunks' AND indexname = 'chunks_embedding_idx'
                """
            )
            row = cursor.fetchone()
    finally:
        conn.close()

    assert row is not None, "HNSW index chunks_embedding_idx not found on chunks table"
    assert "hnsw" in row["indexdef"].lower()
    assert "halfvec_cosine_ops" in row["indexdef"].lower()


def test_chinese_fts_index_exists(fresh_db: None) -> None:
    from knowledge.database import _cursor, connect

    conn = connect()
    try:
        with _cursor(conn) as cursor:
            cursor.execute(
                """
                SELECT indexdef
                FROM pg_indexes
                WHERE tablename = 'chunks' AND indexname = 'chunks_tsv_zh_idx'
                """
            )
            row = cursor.fetchone()
    finally:
        conn.close()

    assert row is not None, "GIN index chunks_tsv_zh_idx not found on chunks table"
    assert "gin" in row["indexdef"].lower()


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


def test_eval_queries_find_expected_mandarin_notes(fresh_db: None, tmp_path: Path) -> None:
    from knowledge.ingest import ingest_directory
    from knowledge.search import search

    # This is a stable synthetic corpus, not the live private notes repo.
    # The paths mirror real note locations so source_path behavior stays realistic.
    notes_dir = tmp_path / "notes"
    _write_mandarin_eval_notes(notes_dir)
    eval_cases = json.loads(
        (FIXTURES_DIR / "chinese_retrieval_eval_queries.json").read_text(encoding="utf-8")
    )

    with patch("knowledge.ingest.get_embeddings", side_effect=_eval_embeddings):
        ingest_result = ingest_directory(notes_dir, glob_pattern="**/*.md")

    assert ingest_result.files_failed == 0

    with patch("knowledge.search.get_embeddings", side_effect=_eval_embeddings):
        for case in eval_cases:
            hits = search(case["query"], limit=1)
            sources = [hit.document.source_path for hit in hits]

            assert any(
                source.endswith(expected)
                for source in sources
                for expected in case["expected_sources"]
            ), f"{case['id']} did not return expected sources. got={sources}"


def _write_mandarin_eval_notes(notes_dir: Path) -> None:
    notes = {
        "areas/mandarin/song-based-character-learning.md": """
# Song-Based Character Learning Plan

The current song sequence includes 学猫叫, 月亮代表我的心, and 童话.
After unsuspending 学猫叫 characters, expect a review spike and monitor retention.
""",
        "areas/mandarin/song-vocabulary-priorities.md": """
# Song Vocabulary Priorities

Worth learning compounds include 内疚, 狼狈, 堕落, and 胆怯.
Literary compounds include 憧憬, 蹒跚, 徜徉, 褴褛, and 聆听.
Onomatopoeia and interjections include 喵, 怦, and 唔.
These are non-RSH characters from songs.
""",
        "areas/mandarin/anki-retention-and-pacing.md": """
# Anki Retention & Pacing Guide

FSRS retention should stay near 85 percent. Use Hard for tone errors and Again for
true failures. Unsuspending 学猫叫 can cause a review spike.
""",
        "areas/mandarin/background.md": """
# Language Background

Cantonese heritage helps Mandarin tone awareness, but interference remains a risk.
""",
        "areas/mandarin/learning-strategy.md": """
# Mandarin Learning Strategy

Milestones include HSK progress, wuxia reading fluency, and Mandarin conversation.
""",
    }

    for relative_path, content in notes.items():
        path = notes_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.strip(), encoding="utf-8")
