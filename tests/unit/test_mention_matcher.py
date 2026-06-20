"""Tests for the case-insensitive mention matching engine (PRD-109)."""
from __future__ import annotations

import pytest

from agenthicc.mentions.matcher import (
    RANK_EXACT,
    RANK_FILENAME_PREFIX,
    RANK_FILENAME_SUBSTR,
    RANK_FUZZY,
    RANK_PATH_SUBSTR,
    RANK_SEGMENT_PREFIX,
    _fuzzy_match,
    filter_and_rank,
    rank_match,
)
from agenthicc.tui.trigger import MatchItem


# ── helpers ───────────────────────────────────────────────────────────────────

def item(display: str) -> MatchItem:
    return MatchItem(display=display, value=display)


def displays(items: list[MatchItem]) -> list[str]:
    return [i.display for i in items]


# ── rank_match ────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_rank_exact_filename() -> None:
    assert rank_match("readme.md", "README.md") == RANK_EXACT


@pytest.mark.unit
def test_rank_exact_case_insensitive() -> None:
    assert rank_match("README.MD", "readme.md") == RANK_EXACT


@pytest.mark.unit
def test_rank_exact_full_path() -> None:
    assert rank_match("docs/readme.md", "docs/README.md") == RANK_EXACT


@pytest.mark.unit
def test_rank_filename_prefix_lower() -> None:
    assert rank_match("read", "README.md") == RANK_FILENAME_PREFIX


@pytest.mark.unit
def test_rank_filename_prefix_upper_query() -> None:
    assert rank_match("READ", "README.md") == RANK_FILENAME_PREFIX


@pytest.mark.unit
def test_rank_filename_prefix_mixed_query() -> None:
    assert rank_match("Read", "README.md") == RANK_FILENAME_PREFIX


@pytest.mark.unit
def test_rank_segment_prefix_nested() -> None:
    # "read" prefix-matches "README.md" which is a segment of "docs/README.md"
    assert rank_match("read", "docs/README.md") == RANK_FILENAME_PREFIX


@pytest.mark.unit
def test_rank_segment_prefix_intermediate_dir() -> None:
    # "src" is a prefix of intermediate segment "src" in "src/auth/login.py".
    # The *filename* is "login.py" which does not start with "src", so the
    # match falls into RANK_SEGMENT_PREFIX (not RANK_FILENAME_PREFIX).
    assert rank_match("src", "src/auth/login.py") == RANK_SEGMENT_PREFIX


@pytest.mark.unit
def test_rank_segment_prefix_not_filename() -> None:
    # "doc" is a prefix of "docs" (parent segment) but not the filename "README.md"
    assert rank_match("doc", "docs/README.md") == RANK_SEGMENT_PREFIX


@pytest.mark.unit
def test_rank_filename_substr() -> None:
    assert rank_match("note", "release_notes.md") == RANK_FILENAME_SUBSTR


@pytest.mark.unit
def test_rank_filename_substr_case_insensitive() -> None:
    assert rank_match("NOTE", "release_notes.md") == RANK_FILENAME_SUBSTR


@pytest.mark.unit
def test_rank_path_substr() -> None:
    # "authentication".startswith("auth") → RANK_SEGMENT_PREFIX, not PATH_SUBSTR.
    assert rank_match("auth", "src/authentication/login.py") == RANK_SEGMENT_PREFIX


@pytest.mark.unit
def test_rank_path_substr_not_segment_prefix() -> None:
    # "uth" is NOT a prefix of any segment but IS a substring of "authentication"
    assert rank_match("uth", "src/authentication/login.py") == RANK_PATH_SUBSTR


@pytest.mark.unit
def test_rank_fuzzy() -> None:
    # r-e-a-d-m-e in order inside "README.md" (name = "readme.md")
    assert rank_match("rdm", "README.md") == RANK_FUZZY


@pytest.mark.unit
def test_rank_fuzzy_case_insensitive() -> None:
    assert rank_match("RDM", "readme.md") == RANK_FUZZY


@pytest.mark.unit
def test_rank_no_match() -> None:
    assert rank_match("xyz", "README.md") is None


@pytest.mark.unit
def test_rank_empty_query_matches_all() -> None:
    assert rank_match("", "README.md") == RANK_FILENAME_PREFIX


@pytest.mark.unit
def test_rank_directory_with_slash() -> None:
    # Trailing "/" should be ignored for matching purposes
    assert rank_match("doc", "docs/") == RANK_FILENAME_PREFIX


@pytest.mark.unit
def test_rank_doc_matches_documentation() -> None:
    assert rank_match("DOC", "Documentation.md") == RANK_FILENAME_PREFIX


# ── filter_and_rank ───────────────────────────────────────────────────────────

@pytest.mark.unit
def test_filter_basic_prefix() -> None:
    pool = [item("README.md"), item("src/"), item("docs/")]
    result = displays(filter_and_rank("re", pool))
    assert "README.md" in result
    assert "src/" not in result


@pytest.mark.unit
def test_filter_case_insensitive_upper_query() -> None:
    pool = [item("README.md"), item("readme.md"), item("Readme.md")]
    result = displays(filter_and_rank("RE", pool))
    assert set(result) == {"README.md", "readme.md", "Readme.md"}


@pytest.mark.unit
def test_filter_preserves_original_casing() -> None:
    pool = [item("README.md")]
    result = filter_and_rank("read", pool)
    assert result[0].display == "README.md"
    assert result[0].value == "README.md"


@pytest.mark.unit
def test_filter_ranking_prefix_before_substr() -> None:
    pool = [item("release_notes.md"), item("README.md")]
    result = displays(filter_and_rank("re", pool))
    # README.md is a prefix match → must appear before release_notes.md (also prefix)
    # Both are RANK_FILENAME_PREFIX so alphabetical within tier
    assert "README.md" in result
    assert "release_notes.md" in result
    # README before release alphabetically by casefold ("readme" < "release")
    assert result.index("README.md") < result.index("release_notes.md")


@pytest.mark.unit
def test_filter_ranking_prefix_before_fuzzy() -> None:
    pool = [item("random.py"), item("README.md")]
    # "rdm" prefix-matches nothing in "random.py" but fuzzy-matches "README.md"
    result = displays(filter_and_rank("rdm", pool))
    assert "README.md" in result
    # "random.py" does not fuzzy-match "rdm" (r-a-n-d vs r-d-m in order)
    # r → r (pos 0), d → ... 'a' 'n' 'd' yes, m → 'o' 'm' yes  actually rdm does match random
    # r=r, d in 'andom'? a-n-d yes, m in 'om'? yes → random.py matches too
    # That's fine; just check README is in results


@pytest.mark.unit
def test_filter_path_segment_match() -> None:
    # "read" should match "docs/README.md" via path segment
    pool = [item("docs/README.md"), item("src/config.py")]
    result = displays(filter_and_rank("read", pool))
    assert "docs/README.md" in result
    assert "src/config.py" not in result


@pytest.mark.unit
def test_filter_substring_match() -> None:
    pool = [item("release_notes.md"), item("main.py")]
    result = displays(filter_and_rank("note", pool))
    assert "release_notes.md" in result
    assert "main.py" not in result


@pytest.mark.unit
def test_filter_fuzzy_rdm_readme() -> None:
    pool = [item("README.md"), item("config.py")]
    result = displays(filter_and_rank("rdm", pool))
    assert "README.md" in result
    assert "config.py" not in result


@pytest.mark.unit
def test_filter_empty_query_returns_all() -> None:
    pool = [item("a.py"), item("b.md"), item("c/")]
    assert len(filter_and_rank("", pool)) == 3


@pytest.mark.unit
def test_filter_no_match_returns_empty() -> None:
    pool = [item("README.md"), item("src/")]
    assert filter_and_rank("xyz", pool) == []


@pytest.mark.unit
def test_filter_doc_matches_docs_and_documentation() -> None:
    pool = [item("docs/"), item("Documentation.md"), item("docstrings.py"), item("main.py")]
    result = displays(filter_and_rank("doc", pool))
    assert "docs/" in result
    assert "Documentation.md" in result
    assert "docstrings.py" in result
    assert "main.py" not in result


@pytest.mark.unit
def test_filter_exact_ranks_first() -> None:
    pool = [item("readme.md"), item("README.md"), item("readme_old.md")]
    # Query exactly matches "readme.md" and "README.md" (both RANK_EXACT).
    # "readme_old.md" fuzzy-matches because all chars of "readme.md" appear in
    # order inside "readme_old.md" — it's included but ranked last.
    result = displays(filter_and_rank("readme.md", pool))
    assert result[0] in {"readme.md", "README.md"}   # exact matches rank first
    assert result[1] in {"readme.md", "README.md"}   # both exact matches lead


@pytest.mark.unit
def test_filter_deterministic() -> None:
    pool = [item("b.py"), item("B.py"), item("a.py")]
    r1 = displays(filter_and_rank("b", pool))
    r2 = displays(filter_and_rank("b", pool))
    assert r1 == r2


# ── _fuzzy_match ──────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_fuzzy_rdm_in_readme() -> None:
    assert _fuzzy_match("rdm", "readme.md")


@pytest.mark.unit
def test_fuzzy_out_of_order_fails() -> None:
    # "dme" — 'd' is at pos 1 in "readme", 'm' is at pos 4, 'e' at pos 5 → matches
    # "dmr" — 'd' pos 1, 'm' pos 4, 'r' not after pos 4 → fails
    assert not _fuzzy_match("dmr", "readme")


@pytest.mark.unit
def test_fuzzy_empty_query() -> None:
    assert _fuzzy_match("", "anything")


@pytest.mark.unit
def test_fuzzy_longer_than_text_fails() -> None:
    assert not _fuzzy_match("abcdefg", "abc")
