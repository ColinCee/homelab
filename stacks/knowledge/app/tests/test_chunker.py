from knowledge.chunker import chunk_text


def test_empty_text_returns_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_short_text_is_single_chunk() -> None:
    text = "Hello world, this is a short paragraph."
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert "Hello world" in chunks[0]


def test_heading_prepended_to_chunks() -> None:
    text = "# My Title\n\nSome content here."
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0].startswith("# My Title")
    assert "Some content here" in chunks[0]


def test_multiple_headings_split_sections() -> None:
    text = "# Section A\n\nContent A.\n\n# Section B\n\nContent B."
    chunks = chunk_text(text)
    assert any("Section A" in c and "Content A" in c for c in chunks)
    assert any("Section B" in c and "Content B" in c for c in chunks)


def test_long_section_splits_into_multiple_chunks() -> None:
    # ~500 tokens ≈ 2000 chars. Create a section well over that.
    paragraph = "This is a test paragraph with enough words. " * 20  # ~900 chars
    text = f"# Big Section\n\n{paragraph}\n\n{paragraph}\n\n{paragraph}\n\n{paragraph}"
    chunks = chunk_text(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.startswith("# Big Section")


def test_heading_prefix_used_for_plain_text() -> None:
    text = "Just some plain text without headings."
    chunks = chunk_text(text, heading_prefix="## Context")
    assert len(chunks) == 1
    assert chunks[0].startswith("## Context")
    assert "plain text" in chunks[0]


def test_heading_prefix_not_used_when_section_has_heading() -> None:
    text = "# Own Heading\n\nBody text."
    chunks = chunk_text(text, heading_prefix="## Fallback")
    assert len(chunks) == 1
    assert chunks[0].startswith("# Own Heading")
    assert "Fallback" not in chunks[0]
