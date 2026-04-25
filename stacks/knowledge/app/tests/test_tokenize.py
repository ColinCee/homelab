import json
from pathlib import Path

from knowledge.tokenize import cjk_search_tokens, english_relaxed_query_text


def test_cjk_search_tokens_segment_no_space_chinese_compounds() -> None:
    # Arrange / Act
    tokens = cjk_search_tokens("内疚狼狈堕落胆怯")

    # Assert
    assert tokens == ["内疚", "狼狈", "堕落", "胆怯"]


def test_cjk_search_tokens_keep_single_character_song_sounds() -> None:
    # Arrange / Act
    tokens = cjk_search_tokens("喵怦唔")

    # Assert
    assert tokens == ["喵", "怦", "唔"]


def test_english_relaxed_query_text_ors_useful_terms() -> None:
    # Arrange / Act
    query = english_relaxed_query_text("FSRS Hard Again tone errors retention 85 percent")

    # Assert
    assert query == "FSRS OR Hard OR Again OR tone OR errors OR retention OR 85 OR percent"


def test_retrieval_eval_fixture_covers_chinese_english_and_mixed_queries() -> None:
    # Arrange
    fixture = Path(__file__).parent / "fixtures" / "chinese_retrieval_eval_queries.json"

    # Act
    cases = json.loads(fixture.read_text(encoding="utf-8"))

    # Assert
    assert {case["language"] for case in cases} == {"zh", "en", "mixed"}
    assert any(case["query"] == "内疚狼狈堕落胆怯" for case in cases)
    assert all(case["expected_sources"] for case in cases)
    assert all(case["expected_terms"] for case in cases)
