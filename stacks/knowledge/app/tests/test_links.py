from unittest.mock import MagicMock, patch
from uuid import UUID

from knowledge.links import (
    _all_similarity_note_links,
    _refresh_similarity_note_links,
    _resolved_wikilink_targets,
    _similarity_note_links,
)
from knowledge.models import Document, NoteLink, RelatedDocument


def test_resolved_wikilink_targets_match_case_insensitive_partial_paths() -> None:
    # Arrange
    source = Document(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        source_path="/notes/source.md",
        title="Source",
        content_hash="hash-source",
    )
    first_target = Document(
        id=UUID("00000000-0000-0000-0000-000000000002"),
        source_path="/notes/projects/Alpha Note.md",
        title="Alpha",
        content_hash="hash-alpha",
    )
    second_target = Document(
        id=UUID("00000000-0000-0000-0000-000000000003"),
        source_path="/notes/reference/beta.md",
        title="Beta",
        content_hash="hash-beta",
    )
    content = (
        "See [[projects/alpha note]], [[BETA]], [[Alpha Note#Overview|alpha]], and [[Missing]]."
    )

    # Act
    targets = _resolved_wikilink_targets(
        content,
        source_document=source,
        documents=[source, first_target, second_target],
    )

    # Assert
    assert [document.source_path for document in targets] == [
        "/notes/projects/Alpha Note.md",
        "/notes/reference/beta.md",
        "/notes/projects/Alpha Note.md",
    ]


@patch("knowledge.links.find_similar_documents")
def test_similarity_note_links_add_bidirectional_edges(
    mock_find_similar_documents: MagicMock,
) -> None:
    # Arrange
    source_id = UUID("00000000-0000-0000-0000-000000000001")
    target_id = UUID("00000000-0000-0000-0000-000000000002")
    source = Document(
        id=source_id,
        source_path="docs/source.md",
        title="Source",
        content_hash="hash-source",
    )
    target = Document(
        id=target_id,
        source_path="docs/target.md",
        title="Target",
        content_hash="hash-target",
    )
    mock_find_similar_documents.return_value = [
        RelatedDocument(
            link_type="similarity",
            score=0.91,
            document=target,
        )
    ]

    # Act
    links = _similarity_note_links(MagicMock(), source_document=source)

    # Assert
    assert {(link.source_id, link.target_id, link.score) for link in links} == {
        (source.id, target.id, 0.91),
        (target.id, source.id, 0.91),
    }


@patch("knowledge.links._similarity_note_links")
@patch("knowledge.links.list_documents")
def test_all_similarity_note_links_deduplicates_edges(
    mock_list_documents: MagicMock,
    mock_similarity_note_links: MagicMock,
) -> None:
    # Arrange
    source_id = UUID("00000000-0000-0000-0000-000000000001")
    target_id = UUID("00000000-0000-0000-0000-000000000002")
    source = Document(
        id=source_id,
        source_path="docs/source.md",
        title="Source",
        content_hash="hash-source",
    )
    target = Document(
        id=target_id,
        source_path="docs/target.md",
        title="Target",
        content_hash="hash-target",
    )
    mock_list_documents.return_value = [source, target]
    mock_similarity_note_links.side_effect = [
        [
            NoteLink(
                source_id=source_id,
                target_id=target_id,
                link_type="similarity",
                score=0.91,
            ),
            NoteLink(
                source_id=target_id,
                target_id=source_id,
                link_type="similarity",
                score=0.91,
            ),
        ],
        [
            NoteLink(
                source_id=target_id,
                target_id=source_id,
                link_type="similarity",
                score=0.91,
            ),
            NoteLink(
                source_id=source_id,
                target_id=target_id,
                link_type="similarity",
                score=0.91,
            ),
        ],
    ]
    conn = MagicMock()

    # Act
    links = _all_similarity_note_links(conn)

    # Assert
    assert {(link.source_id, link.target_id) for link in links} == {
        (source_id, target_id),
        (target_id, source_id),
    }
    mock_list_documents.assert_called_once_with(conn)
    assert [call.args[0] for call in mock_similarity_note_links.call_args_list] == [conn, conn]
    source_documents = [
        call.kwargs["source_document"] for call in mock_similarity_note_links.call_args_list
    ]
    assert source_documents == [source, target]


@patch("knowledge.links.insert_note_links")
@patch("knowledge.links.delete_note_links")
@patch("knowledge.links._all_similarity_note_links")
def test_refresh_similarity_note_links_rebuilds_graph(
    mock_all_similarity_note_links: MagicMock,
    mock_delete_note_links: MagicMock,
    mock_insert_note_links: MagicMock,
) -> None:
    # Arrange
    conn = MagicMock()
    links = [
        MagicMock(),
        MagicMock(),
    ]
    mock_all_similarity_note_links.return_value = links

    # Act
    _refresh_similarity_note_links(conn)

    # Assert
    mock_delete_note_links.assert_called_once_with(conn, link_type="similarity")
    mock_all_similarity_note_links.assert_called_once_with(conn)
    mock_insert_note_links.assert_called_once_with(conn, links)
