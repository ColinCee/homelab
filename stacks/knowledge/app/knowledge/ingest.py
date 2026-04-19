"""Ingestion orchestrator: load → chunk → embed → store."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from .chunker import chunk_text
from .database import (
    DatabaseConnection,
    connect,
    create_workspace,
    delete_document_chunks,
    get_document_by_source,
    insert_chunks,
    upsert_document,
)
from .embeddings import get_embeddings
from .models import Chunk, Document, IngestResult, Workspace

logger = logging.getLogger(__name__)


def ingest_file(
    path: Path,
    *,
    workspace: str,
    conn: DatabaseConnection | None = None,
    token: str | None = None,
) -> IngestResult:
    """Ingest a single file into the knowledge base."""
    content = path.read_text(encoding="utf-8")
    title = _title_from_file(path, content)
    source_path = str(path)
    return _ingest(
        content=content,
        title=title,
        source_path=source_path,
        workspace=workspace,
        conn=conn,
        token=token,
    )


def ingest_text(
    text: str,
    *,
    title: str,
    workspace: str,
    source_id: str | None = None,
    conn: DatabaseConnection | None = None,
    token: str | None = None,
) -> IngestResult:
    """Ingest raw text as a document.

    *source_id* disambiguates notes with the same title. When omitted the
    content hash is used, so identical titles with different content won't
    collide.
    """
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    key = source_id or content_hash[:12]
    return _ingest(
        content=text,
        title=title,
        source_path=f"text://{title}/{key}",
        workspace=workspace,
        conn=conn,
        token=token,
    )


def _ingest(
    *,
    content: str,
    title: str,
    source_path: str,
    workspace: str,
    conn: DatabaseConnection | None,
    token: str | None,
) -> IngestResult:
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    own_conn = conn is None
    db = conn or connect()

    try:
        return _do_ingest(
            db,
            content=content,
            title=title,
            source_path=source_path,
            workspace=workspace,
            content_hash=content_hash,
            token=token,
        )
    finally:
        if own_conn:
            db.close()


def _do_ingest(
    conn: DatabaseConnection,
    *,
    content: str,
    title: str,
    source_path: str,
    workspace: str,
    content_hash: str,
    token: str | None,
) -> IngestResult:
    # Ensure workspace exists
    create_workspace(conn, Workspace(name=workspace))

    # Check for unchanged content
    existing = _find_existing(conn, workspace, source_path)
    if existing and existing.content_hash == content_hash:
        logger.info("Skipping unchanged document: %s", source_path)
        conn.commit()
        return IngestResult(
            workspace=workspace,
            documents_processed=0,
            chunks_created=0,
            documents_skipped=1,
        )

    # Chunk the content
    chunks_text = chunk_text(content)
    if not chunks_text:
        logger.warning("No chunks produced from: %s", source_path)
        # Still update the document and delete stale chunks so search
        # doesn't return content from a previous version.
        if existing:
            doc = Document(
                workspace=workspace,
                source_path=source_path,
                title=title,
                content_hash=content_hash,
            )
            saved_doc = upsert_document(conn, doc)
            delete_document_chunks(conn, saved_doc)
        conn.commit()
        return IngestResult(
            workspace=workspace,
            documents_processed=1 if existing else 0,
            chunks_created=0,
            documents_skipped=0,
        )

    # Embed all chunks
    embeddings = get_embeddings(chunks_text, token=token)

    # Store in a single transaction
    doc = Document(
        workspace=workspace,
        source_path=source_path,
        title=title,
        content_hash=content_hash,
    )
    saved_doc = upsert_document(conn, doc)
    assert saved_doc.id is not None, "upsert_document must return a document with an ID"

    # Delete old chunks if re-ingesting
    if existing:
        delete_document_chunks(conn, saved_doc)

    chunk_models = [
        Chunk(
            document_id=saved_doc.id,
            chunk_index=i,
            content=text,
            embedding=embedding,
        )
        for i, (text, embedding) in enumerate(zip(chunks_text, embeddings, strict=True))
    ]
    inserted = insert_chunks(conn, chunk_models)
    conn.commit()

    logger.info(
        "Ingested %s: %d chunks into workspace '%s'",
        source_path,
        len(inserted),
        workspace,
    )
    return IngestResult(
        workspace=workspace,
        documents_processed=1,
        chunks_created=len(inserted),
        documents_skipped=0,
    )


def _find_existing(conn: DatabaseConnection, workspace: str, source_path: str) -> Document | None:
    """Find an existing document by workspace + source_path."""
    return get_document_by_source(conn, workspace, source_path)


def _title_from_file(path: Path, content: str) -> str:
    """Derive title from first markdown heading or filename."""
    if path.suffix in (".md", ".markdown"):
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()

    return path.stem.replace("-", " ").replace("_", " ").title()
