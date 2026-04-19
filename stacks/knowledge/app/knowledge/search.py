from __future__ import annotations

from .database import DatabaseConnection, connect, search_chunks
from .embeddings import get_embeddings
from .models import SearchResult

DEFAULT_RESULT_LIMIT = 5
_EXCERPT_LENGTH = 2000


def search(
    query: str,
    *,
    limit: int = DEFAULT_RESULT_LIMIT,
    conn: DatabaseConnection | None = None,
    token: str | None = None,
) -> list[SearchResult]:
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("query must not be blank")

    own_conn = conn is None
    db = conn or connect()

    try:
        query_embedding = _embed_query(normalized_query, token=token)
        return search_chunks(db, query_embedding, limit=limit, query_text=normalized_query)
    finally:
        if own_conn:
            db.close()


def format_search_results(results: list[SearchResult]) -> str:
    if not results:
        return "No matching chunks found."

    return "\n\n".join(
        _format_result(index, result) for index, result in enumerate(results, start=1)
    )


def _embed_query(query: str, *, token: str | None = None) -> list[float]:
    embeddings = get_embeddings([query], token=token)
    if len(embeddings) != 1:
        raise RuntimeError(f"expected 1 embedding for query, got {len(embeddings)}")
    return embeddings[0]


def _format_result(index: int, result: SearchResult) -> str:
    header = (
        f"{index}. score={result.score:.3f} "
        f"source={result.document.source_path} "
        f"chunk={result.chunk.chunk_index}"
    )
    excerpt = _excerpt(result.chunk.content)
    return f"{header}\n   {excerpt}"


def _excerpt(content: str, *, limit: int = _EXCERPT_LENGTH) -> str:
    normalized = " ".join(content.split())
    if len(normalized) <= limit:
        return normalized

    return f"{normalized[: limit - 3].rstrip()}..."
