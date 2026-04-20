"""Ingestion orchestrator: load → chunk → embed → store."""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from uuid import UUID

from .chunker import chunk_text
from .database import (
    DatabaseConnection,
    connect,
    delete_document,
    delete_document_chunks,
    delete_note_links_for_source,
    delete_similarity_links_for_document,
    find_similar_documents,
    get_document_by_source,
    insert_chunks,
    insert_note_links,
    list_documents,
    list_documents_by_source_prefix,
    upsert_document,
)
from .embeddings import get_embeddings
from .models import Chunk, DirectoryIngestResult, Document, IngestResult, NoteLink

logger = logging.getLogger(__name__)

DEFAULT_DIRECTORY_GLOB = "**/*.md"
_DEFAULT_DIRECTORY_EXTRA_GLOBS = ("**/*.txt", "**/*.pdf")
_INGESTIBLE_SUFFIXES = frozenset({".md", ".txt", ".pdf"})
_SIMILARITY_LIMIT = 5
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")


def ingest_file(
    path: Path,
    *,
    conn: DatabaseConnection | None = None,
    token: str | None = None,
) -> IngestResult:
    """Ingest a single file into the knowledge base."""
    content = _read_file_content(path)
    content_hash = _file_content_hash(path)
    title = _title_from_file(path, content)
    source_path = str(path)
    return _ingest(
        content=content,
        title=title,
        source_path=source_path,
        content_hash=content_hash,
        conn=conn,
        token=token,
    )


def ingest_text(
    text: str,
    *,
    title: str,
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
        conn=conn,
        token=token,
    )


def ingest_directory(
    directory: Path,
    *,
    glob_pattern: str = DEFAULT_DIRECTORY_GLOB,
    conn: DatabaseConnection | None = None,
    token: str | None = None,
) -> DirectoryIngestResult:
    """Ingest all supported files in a directory tree."""
    normalized_directory = directory.expanduser().resolve()
    own_conn = conn is None
    db = conn or connect()

    try:
        return _do_directory_ingest(
            db,
            directory=normalized_directory,
            glob_pattern=glob_pattern,
            token=token,
        )
    finally:
        if own_conn:
            db.close()


def _ingest(
    *,
    content: str,
    title: str,
    source_path: str,
    conn: DatabaseConnection | None,
    token: str | None,
    content_hash: str | None = None,
) -> IngestResult:
    if content_hash is None:
        content_hash = hashlib.sha256(content.encode()).hexdigest()
    own_conn = conn is None
    db = conn or connect()

    try:
        return _do_ingest(
            db,
            content=content,
            title=title,
            source_path=source_path,
            content_hash=content_hash,
            token=token,
        )
    except BaseException:
        if not own_conn:
            db.rollback()
        raise
    finally:
        if own_conn:
            db.close()


def _do_ingest(
    conn: DatabaseConnection,
    *,
    content: str,
    title: str,
    source_path: str,
    content_hash: str,
    token: str | None,
) -> IngestResult:
    # Check for unchanged content
    existing = _find_existing(conn, source_path)
    if existing and existing.content_hash == content_hash:
        logger.info("Skipping unchanged document: %s", source_path)
        conn.commit()
        return IngestResult(
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
                source_path=source_path,
                title=title,
                content_hash=content_hash,
            )
            saved_doc = upsert_document(conn, doc)
            delete_document_chunks(conn, saved_doc)
            _refresh_note_links(conn, document=saved_doc, content=content)
        conn.commit()
        return IngestResult(
            documents_processed=1 if existing else 0,
            chunks_created=0,
            documents_skipped=0,
        )

    # Embed all chunks
    embeddings = get_embeddings(chunks_text, token=token)

    # Store in a single transaction
    doc = Document(
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
    _refresh_note_links(conn, document=saved_doc, content=content)
    conn.commit()

    logger.info("Ingested %s: %d chunks", source_path, len(inserted))
    return IngestResult(
        documents_processed=1,
        chunks_created=len(inserted),
        documents_skipped=0,
    )


def _find_existing(conn: DatabaseConnection, source_path: str) -> Document | None:
    """Find an existing document by source_path."""
    return get_document_by_source(conn, source_path)


def _refresh_note_links(
    conn: DatabaseConnection,
    *,
    document: Document,
    content: str,
) -> None:
    delete_note_links_for_source(conn, document, link_type="wikilink")
    delete_similarity_links_for_document(conn, document)
    insert_note_links(conn, _wikilink_note_links(content, source_document=document, conn=conn))
    insert_note_links(conn, _similarity_note_links(conn, source_document=document))


def _wikilink_note_links(
    content: str,
    *,
    source_document: Document,
    conn: DatabaseConnection,
) -> list[NoteLink]:
    source_id = _require_document_id(source_document)
    documents = list_documents(conn)
    links: list[NoteLink] = []
    seen_targets: set[UUID] = set()

    for target in _resolved_wikilink_targets(
        content,
        source_document=source_document,
        documents=documents,
    ):
        target_id = _require_document_id(target)
        if target_id in seen_targets:
            continue
        seen_targets.add(target_id)
        links.append(
            NoteLink(
                source_id=source_id,
                target_id=target_id,
                link_type="wikilink",
                score=None,
            )
        )

    return links


def _similarity_note_links(
    conn: DatabaseConnection,
    *,
    source_document: Document,
) -> list[NoteLink]:
    source_id = _require_document_id(source_document)
    links_by_key: dict[tuple[UUID, UUID], NoteLink] = {}

    for related in find_similar_documents(conn, source_document, limit=_SIMILARITY_LIMIT):
        target_id = _require_document_id(related.document)
        if target_id == source_id:
            continue

        for left, right in ((source_id, target_id), (target_id, source_id)):
            links_by_key[(left, right)] = NoteLink(
                source_id=left,
                target_id=right,
                link_type="similarity",
                score=related.score,
            )

    return list(links_by_key.values())


def _resolved_wikilink_targets(
    content: str,
    *,
    source_document: Document,
    documents: list[Document],
) -> list[Document]:
    targets: list[Document] = []

    for wikilink in _extract_wikilinks(content):
        target = _resolve_wikilink_target(
            wikilink,
            source_document=source_document,
            documents=documents,
        )
        if target is not None:
            targets.append(target)

    return targets


def _extract_wikilinks(content: str) -> list[str]:
    links: list[str] = []
    for raw_match in _WIKILINK_RE.findall(content):
        target = raw_match.split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            links.append(target)
    return links


def _resolve_wikilink_target(
    wikilink: str,
    *,
    source_document: Document,
    documents: list[Document],
) -> Document | None:
    matches: list[tuple[int, int, str, Document]] = []
    candidate_full = _normalize_path(wikilink)
    candidate_stem = _without_markdown_suffix(candidate_full)

    for document in documents:
        if document.id == source_document.id:
            continue

        rank = _match_rank(
            candidate_full=candidate_full,
            candidate_stem=candidate_stem,
            source_path=document.source_path,
        )
        if rank is None:
            continue

        matches.append((rank, len(document.source_path), document.source_path, document))

    if not matches:
        return None

    matches.sort(key=lambda item: item[:3])
    return matches[0][3]


def _match_rank(
    *,
    candidate_full: str,
    candidate_stem: str,
    source_path: str,
) -> int | None:
    document_full = _normalize_path(source_path)
    document_stem = _without_markdown_suffix(document_full)
    basename_full = document_full.rsplit("/", 1)[-1]
    basename_stem = _without_markdown_suffix(basename_full)
    suffix_stem = f"/{candidate_stem}"
    suffix_full = f"/{candidate_full}"

    checks = (
        document_stem == candidate_stem,
        document_full == candidate_full,
        document_stem.endswith(suffix_stem),
        document_full.endswith(suffix_full),
        basename_stem == candidate_stem,
        basename_full == candidate_full,
    )
    for index, matched in enumerate(checks):
        if matched:
            return index
    return None


def _normalize_path(value: str) -> str:
    return value.strip().replace("\\", "/").strip("/").lower()


def _without_markdown_suffix(value: str) -> str:
    if value.endswith(".md"):
        return value[:-3]
    return value


def _do_directory_ingest(
    conn: DatabaseConnection,
    *,
    directory: Path,
    glob_pattern: str,
    token: str | None,
) -> DirectoryIngestResult:
    files = _iter_directory_files(directory, glob_pattern)
    live_paths = {str(path) for path in _iter_supported_directory_files(directory)}

    documents_processed = 0
    chunks_created = 0
    documents_skipped = 0
    files_failed = 0
    consecutive_failures = 0
    max_consecutive_failures = 5

    for path in files:
        try:
            result = ingest_file(path, conn=conn, token=token)
        except Exception:
            conn.rollback()
            logger.exception("Failed to ingest %s", path)
            files_failed += 1
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                logger.error(
                    "Aborting: %d consecutive failures — likely a systemic issue",
                    consecutive_failures,
                )
                break
            continue

        consecutive_failures = 0
        documents_processed += result.documents_processed
        chunks_created += result.chunks_created
        documents_skipped += result.documents_skipped

    documents_deleted = _delete_orphaned_documents(
        conn,
        directory_prefix=_source_prefix_for_directory(directory),
        live_paths=live_paths,
    )
    return DirectoryIngestResult(
        files_found=len(files),
        files_failed=files_failed,
        documents_processed=documents_processed,
        chunks_created=chunks_created,
        documents_skipped=documents_skipped,
        documents_deleted=documents_deleted,
    )


def _iter_directory_files(directory: Path, glob_pattern: str) -> list[Path]:
    matched_paths: set[str] = set()

    for pattern in _directory_globs(glob_pattern):
        for path in directory.glob(pattern):
            resolved_path = str(Path(path.resolve()))
            resolved_file = Path(resolved_path)
            if not resolved_file.is_file():
                continue
            if resolved_file.suffix.lower() not in _INGESTIBLE_SUFFIXES:
                continue
            matched_paths.add(resolved_path)

    return [Path(path) for path in sorted(matched_paths)]


def _iter_supported_directory_files(directory: Path) -> list[Path]:
    matched_paths: set[str] = set()

    for path in directory.rglob("*"):
        resolved_path = str(Path(path.resolve()))
        resolved_file = Path(resolved_path)
        if not resolved_file.is_file():
            continue
        if resolved_file.suffix.lower() not in _INGESTIBLE_SUFFIXES:
            continue
        matched_paths.add(resolved_path)

    return [Path(path) for path in sorted(matched_paths)]


def _directory_globs(glob_pattern: str) -> tuple[str, ...]:
    if glob_pattern == DEFAULT_DIRECTORY_GLOB:
        return (glob_pattern, *_DEFAULT_DIRECTORY_EXTRA_GLOBS)
    return (glob_pattern,)


def _delete_orphaned_documents(
    conn: DatabaseConnection,
    *,
    directory_prefix: str,
    live_paths: set[str],
) -> int:
    deleted = 0

    for document in list_documents_by_source_prefix(conn, directory_prefix):
        if document.source_path in live_paths:
            continue

        try:
            deleted += delete_document(conn, document)
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("Failed to delete orphaned document: %s", document.source_path)

    return deleted


def _source_prefix_for_directory(directory: Path) -> str:
    return f"{directory.as_posix().rstrip('/')}/"


def _require_document_id(document: Document) -> UUID:
    if document.id is None:
        raise ValueError("document.id must be set before creating note links")
    return document.id


def _file_content_hash(path: Path) -> str:
    """Hash raw file bytes for change detection (catches binary-only PDF changes)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_file_content(path: Path) -> str:
    """Read file content, extracting text from PDFs."""
    if path.suffix.lower() == ".pdf":
        return _extract_pdf_text(path)
    return path.read_text(encoding="utf-8")


def _extract_pdf_text(path: Path) -> str:
    """Extract text from a PDF using pypdf."""
    from pypdf import PdfReader

    reader = PdfReader(path)
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(page for page in pages if page.strip())


def _title_from_file(path: Path, content: str) -> str:
    """Derive title from first markdown heading or filename."""
    if path.suffix in (".md", ".markdown"):
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()

    return path.stem.replace("-", " ").replace("_", " ").title()
