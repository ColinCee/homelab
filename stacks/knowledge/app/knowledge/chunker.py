"""Split text into overlapping chunks, preserving markdown heading context."""

from __future__ import annotations

import re

_TARGET_TOKENS = 500
_OVERLAP_TOKENS = 50
# Rough approximation: 1 token ≈ 4 chars (GPT-family tokenizers).
_CHARS_PER_TOKEN = 4
_TARGET_CHARS = _TARGET_TOKENS * _CHARS_PER_TOKEN
_OVERLAP_CHARS = _OVERLAP_TOKENS * _CHARS_PER_TOKEN

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def chunk_text(text: str, *, heading_prefix: str = "") -> list[str]:
    """Split *text* into chunks of roughly _TARGET_TOKENS tokens with _OVERLAP_TOKENS overlap.

    If *heading_prefix* is provided it is prepended to every chunk so the
    embedding model has section context.
    """
    if not text.strip():
        return []

    sections = _split_by_headings(text)
    raw_chunks: list[str] = []

    for section_heading, section_body in sections:
        prefix = section_heading or heading_prefix
        _chunk_section(section_body, prefix=prefix, out=raw_chunks)

    return [c for c in raw_chunks if c.strip()]


def _split_by_headings(text: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) pairs.

    Non-headed text at the top gets an empty heading.
    """
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [("", text)]

    sections: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        sections.append(("", text[: matches[0].start()]))

    for i, match in enumerate(matches):
        heading = match.group(0).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((heading, text[start:end]))

    return sections


def _chunk_section(body: str, *, prefix: str, out: list[str]) -> None:
    """Chunk a single section's body, prepending *prefix* to each chunk."""
    body = body.strip()
    if not body:
        return

    prefix_str = f"{prefix}\n\n" if prefix else ""

    if len(body) <= _TARGET_CHARS:
        out.append(f"{prefix_str}{body}")
        return

    paragraphs = re.split(r"\n{2,}", body)
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_len = len(para)

        if current and current_len + para_len > _TARGET_CHARS:
            out.append(f"{prefix_str}{_join(current)}")
            # Keep overlap from the end of current chunk
            current, current_len = _take_overlap(current)

        current.append(para)
        current_len += para_len

    if current:
        out.append(f"{prefix_str}{_join(current)}")


def _join(paragraphs: list[str]) -> str:
    return "\n\n".join(paragraphs)


def _take_overlap(paragraphs: list[str]) -> tuple[list[str], int]:
    """Return paragraphs from the end that fit within the overlap budget."""
    overlap: list[str] = []
    total = 0
    for para in reversed(paragraphs):
        if total + len(para) > _OVERLAP_CHARS:
            break
        overlap.insert(0, para)
        total += len(para)
    return overlap, total
