from __future__ import annotations

import re

import jieba

_CJK_SPAN_RE = re.compile(r"[\u3400-\u9fff]+")
_ENGLISH_TERM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#._-]*")

_CJK_STOPWORDS = frozenset({"的", "了", "我", "是"})
_ENGLISH_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)


def cjk_search_text(text: str) -> str:
    """Return whitespace-separated Chinese search tokens for Postgres simple FTS."""

    return " ".join(cjk_search_tokens(text))


def cjk_search_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for span in _CJK_SPAN_RE.findall(text):
        for token in jieba.cut_for_search(span):
            normalized = token.strip()
            if not normalized or normalized in _CJK_STOPWORDS or normalized in seen:
                continue
            seen.add(normalized)
            tokens.append(normalized)

    return tokens


def english_relaxed_query_text(text: str) -> str:
    """Return a safe websearch query that ORs useful English terms."""

    terms: list[str] = []
    seen: set[str] = set()
    for match in _ENGLISH_TERM_RE.finditer(text):
        term = _normalize_english_term(match.group(0))
        if not term or term.lower() in _ENGLISH_STOPWORDS or term.lower() in seen:
            continue
        seen.add(term.lower())
        terms.append(term)

    return " OR ".join(terms)


def _normalize_english_term(term: str) -> str:
    return term.strip("._-+#")
