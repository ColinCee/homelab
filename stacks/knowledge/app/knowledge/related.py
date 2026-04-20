from __future__ import annotations

from .database import (
    DatabaseConnection,
    get_document_by_source,
    list_related_documents,
    managed_connection,
)
from .models import RelatedDocument


def related(
    source_path: str,
    *,
    conn: DatabaseConnection | None = None,
) -> list[RelatedDocument]:
    normalized_source_path = source_path.strip()
    if not normalized_source_path:
        raise ValueError("source_path must not be blank")

    with managed_connection(conn) as db:
        document = get_document_by_source(db, normalized_source_path)
        if document is None:
            raise ValueError(f"document not found: {normalized_source_path}")
        return list_related_documents(db, document)


def format_related_results(results: list[RelatedDocument]) -> str:
    if not results:
        return "No related documents found."

    return "\n".join(
        _format_related_result(index, result) for index, result in enumerate(results, start=1)
    )


def _format_related_result(index: int, result: RelatedDocument) -> str:
    score = "-" if result.score is None else f"{result.score:.3f}"
    return f"{index}. type={result.link_type} score={score} source={result.document.source_path}"
