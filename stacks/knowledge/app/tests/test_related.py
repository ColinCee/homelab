import json
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from knowledge import __main__ as cli
from knowledge.models import Document, NoteLinkType, RelatedDocument
from knowledge.related import format_related_results, related


def _related_document(
    *,
    source_path: str,
    link_type: NoteLinkType,
    score: float | None,
) -> RelatedDocument:
    return RelatedDocument(
        link_type=link_type,
        score=score,
        document=Document(
            id=UUID("00000000-0000-0000-0000-000000000099"),
            source_path=source_path,
            title="Target",
            content_hash="hash-target",
            ingested_at=datetime(2026, 4, 19, tzinfo=UTC),
        ),
    )


def _task_event(stderr: str) -> dict[str, object]:
    return json.loads(stderr.strip())


def test_format_related_results_includes_link_type_and_score() -> None:
    # Arrange
    results = [
        _related_document(source_path="docs/linked.md", link_type="wikilink", score=None),
        _related_document(source_path="docs/similar.md", link_type="similarity", score=0.8731),
    ]

    # Act
    formatted = format_related_results(results)

    # Assert
    assert formatted == (
        "1. type=wikilink score=- source=docs/linked.md\n"
        "2. type=similarity score=0.873 source=docs/similar.md"
    )


def test_format_related_results_handles_empty_results() -> None:
    # Arrange / Act
    formatted = format_related_results([])

    # Assert
    assert formatted == "No related documents found."


@patch("knowledge.related.connect")
@patch("knowledge.related.list_related_documents")
@patch("knowledge.related.get_document_by_source")
def test_related_returns_linked_documents(
    mock_get_document_by_source: MagicMock,
    mock_list_related_documents: MagicMock,
    mock_connect: MagicMock,
) -> None:
    # Arrange
    conn = MagicMock()
    source = Document(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        source_path="docs/source.md",
        title="Source",
        content_hash="hash-source",
    )
    mock_connect.return_value = conn
    mock_get_document_by_source.return_value = source
    mock_list_related_documents.return_value = [
        _related_document(source_path="docs/linked.md", link_type="wikilink", score=None)
    ]

    # Act
    results = related("  docs/source.md  ")

    # Assert
    mock_get_document_by_source.assert_called_once_with(conn, "docs/source.md")
    mock_list_related_documents.assert_called_once_with(conn, source)
    conn.close.assert_called_once_with()
    assert results[0].document.source_path == "docs/linked.md"


def test_related_rejects_unknown_document() -> None:
    # Arrange / Act / Assert
    with patch("knowledge.related.get_document_by_source", return_value=None), pytest.raises(
        ValueError, match=r"document not found: docs/missing\.md"
    ):
        related("docs/missing.md", conn=MagicMock())


def test_cli_related_prints_results(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Arrange
    expected = [_related_document(source_path="docs/linked.md", link_type="wikilink", score=None)]
    calls: dict[str, object] = {}

    def fake_related(source_path: str) -> list[RelatedDocument]:
        calls["source_path"] = source_path
        return expected

    monkeypatch.setattr(cli, "connect", lambda: MagicMock())
    monkeypatch.setattr(cli, "run_migrations", lambda conn: None)
    monkeypatch.setattr(cli, "related", fake_related)
    monkeypatch.setattr(
        sys,
        "argv",
        ["knowledge", "related", "docs/source.md"],
    )

    # Act
    cli.main()

    # Assert
    assert calls == {"source_path": "docs/source.md"}
    captured = capsys.readouterr()
    assert captured.out.strip() == "1. type=wikilink score=- source=docs/linked.md"
    assert _task_event(captured.err) == {
        "command": "related",
        "event": "task_completed",
        "exit_code": 0,
        "result_count": 1,
        "status": "succeeded",
        "duration_seconds": pytest.approx(0, abs=1),
    }
