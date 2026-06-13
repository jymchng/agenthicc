from __future__ import annotations

import pytest
from pathlib import Path

from agenthicc.mentions.cache import MentionCache, _sha256_file

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# PRD-35 core tests
# ---------------------------------------------------------------------------

def test_record_and_is_unchanged(tmp_path: Path) -> None:
    f = tmp_path / "auth.py"
    f.write_text("x = 1")
    cache = MentionCache()
    cache.record("auth.py", f, chars_used=5, turn_index=0)
    assert cache.is_unchanged("auth.py", f)


def test_is_changed_after_file_modified(tmp_path: Path) -> None:
    f = tmp_path / "auth.py"
    f.write_text("x = 1")
    cache = MentionCache()
    cache.record("auth.py", f, chars_used=5, turn_index=0)
    f.write_text("x = 2")  # modify
    assert not cache.is_unchanged("auth.py", f)


def test_last_turn_returns_none_for_unknown() -> None:
    cache = MentionCache()
    assert cache.last_turn("unknown.py") is None


def test_clear_empties_cache(tmp_path: Path) -> None:
    f = tmp_path / "f.py"
    f.write_text("y")
    cache = MentionCache()
    cache.record("f.py", f, 1, 0)
    cache.clear()
    assert not cache.is_unchanged("f.py", f)


# ---------------------------------------------------------------------------
# URL cache tests (G8)
# ---------------------------------------------------------------------------

def test_url_cache_get_returns_none_when_missing() -> None:
    cache = MentionCache()
    assert cache.get_url("https://example.com") is None


def test_url_cache_set_and_get() -> None:
    cache = MentionCache()
    block = '<url href="https://example.com">\nhello\n</url>'
    cache.set_url("https://example.com", block)
    assert cache.get_url("https://example.com") == block


def test_url_cache_cleared_on_clear() -> None:
    cache = MentionCache()
    cache.set_url("https://example.com", "block")
    cache.clear()
    assert cache.get_url("https://example.com") is None


# ---------------------------------------------------------------------------
# Extra coverage tests
# ---------------------------------------------------------------------------

def test_record_overwrites_previous_entry(tmp_path: Path) -> None:
    """Recording the same path twice — the most recent entry wins."""
    f = tmp_path / "mod.py"
    f.write_text("v1")
    cache = MentionCache()
    cache.record("mod.py", f, chars_used=2, turn_index=0)

    f.write_text("v2")
    cache.record("mod.py", f, chars_used=2, turn_index=3)

    # last_turn should reflect the second record call
    assert cache.last_turn("mod.py") == 3
    # is_unchanged should reflect the hash captured at the second record
    assert cache.is_unchanged("mod.py", f)


def test_is_unchanged_returns_false_for_unknown_path(tmp_path: Path) -> None:
    f = tmp_path / "never_recorded.py"
    f.write_text("content")
    cache = MentionCache()
    assert not cache.is_unchanged("never_recorded.py", f)


def test_sha256_file_empty_path_returns_empty_string() -> None:
    """_sha256_file returns '' for a non-existent path (OSError branch)."""
    missing = Path("/tmp/__agenthicc_no_such_file_12345678.py")
    assert _sha256_file(missing) == ""
