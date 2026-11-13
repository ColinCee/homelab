from checks.docs_size import grown


def test_growth_and_new_docs_fail_shrinking_passes() -> None:
    baseline = {"a.md": 100, "b.md": 50}
    sizes = {"a.md": 101, "b.md": 40, "c.md": 5}
    assert grown(sizes, baseline) == [
        "a.md: 101 words, cap 100",
        "c.md: 5 words, not in docs-size.json",
    ]


def test_unchanged_docs_pass() -> None:
    assert grown({"a.md": 100}, {"a.md": 100}) == []
