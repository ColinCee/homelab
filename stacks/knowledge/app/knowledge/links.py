"""Note link graph: wikilink resolution and similarity computation."""

from __future__ import annotations

import re
from uuid import UUID

from .database import (
    DatabaseConnection,
    _require_document_id,
    delete_note_links,
    delete_note_links_for_source,
    find_similar_documents,
    insert_note_links,
    list_documents,
)
from .models import Document, NoteLink

_SIMILARITY_LIMIT = 5
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")


def refresh_note_links(
    conn: DatabaseConnection,
    *,
    document: Document,
    content: str,
) -> None:
    """Rebuild wikilink and similarity edges for a document."""
    delete_note_links_for_source(conn, document, link_type="wikilink")
    insert_note_links(conn, _wikilink_note_links(content, source_document=document, conn=conn))
    _refresh_similarity_note_links(conn)


def _refresh_similarity_note_links(conn: DatabaseConnection) -> None:
    delete_note_links(conn, link_type="similarity")
    insert_note_links(conn, _all_similarity_note_links(conn))


def _all_similarity_note_links(conn: DatabaseConnection) -> list[NoteLink]:
    links_by_key: dict[tuple[UUID, UUID], NoteLink] = {}

    for document in list_documents(conn):
        for link in _similarity_note_links(conn, source_document=document):
            links_by_key[(link.source_id, link.target_id)] = link

    return list(links_by_key.values())


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
