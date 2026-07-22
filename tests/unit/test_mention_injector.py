"""Tests for @mention content injection (PRD-33 + PRD-35)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agenthicc.mentions.cache import MentionCache
from agenthicc.mentions.injector import (
    InjectionConfig,
    InjectedContent,
    build_context_prefix,
    resolve_mention,
)
from agenthicc.mentions.parser import Mention, MentionKind

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_mention(tmp_path: Path, filename: str, content: str) -> Mention:
    p = tmp_path / filename
    p.write_text(content, encoding="utf-8")
    return Mention(
        raw=f"@{filename}",
        path=filename,
        kind=MentionKind.FILE,
        resolved=p.resolve(),
        start=0,
        end=len(filename) + 1,
    )


def _unresolved(name: str) -> Mention:
    return Mention(
        raw=f"@{name}",
        path=name,
        kind=MentionKind.UNRESOLVED,
        resolved=None,
        start=0,
        end=len(name) + 1,
    )


# ---------------------------------------------------------------------------
# PRD-33 base tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_file_mention(tmp_path: Path) -> None:
    m = _file_mention(tmp_path, "hello.py", "print('hi')")
    cfg = InjectionConfig(cwd=tmp_path)
    result = await resolve_mention(m, cfg)
    assert result.ok
    assert "hello.py" in result.block
    assert "print('hi')" in result.block


@pytest.mark.asyncio
async def test_resolve_file_truncates_large_file(tmp_path: Path) -> None:
    m = _file_mention(tmp_path, "big.py", "x" * 50_000)
    cfg = InjectionConfig(cwd=tmp_path, max_file_chars=100)
    result = await resolve_mention(m, cfg)
    assert "truncated" in result.block
    assert len(result.block) < 1000


@pytest.mark.asyncio
async def test_resolve_unresolved_mention(tmp_path: Path) -> None:
    m = _unresolved("ghost.py")
    result = await resolve_mention(m, InjectionConfig(cwd=tmp_path))
    assert result.error == "not_found"
    assert "not found" in result.block


@pytest.mark.asyncio
async def test_resolve_directory_mention(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("")
    m = Mention(
        raw="@src/",
        path="src/",
        kind=MentionKind.DIRECTORY,
        resolved=(tmp_path / "src").resolve(),
        start=0,
        end=5,
    )
    result = await resolve_mention(m, InjectionConfig(cwd=tmp_path))
    assert "main.py" in result.block
    assert "<dir" in result.block


@pytest.mark.asyncio
async def test_binary_file_shows_placeholder(tmp_path: Path) -> None:
    p = tmp_path / "image.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    m = Mention(
        raw="@image.png",
        path="image.png",
        kind=MentionKind.FILE,
        resolved=p.resolve(),
        start=0,
        end=10,
    )
    result = await resolve_mention(m, InjectionConfig(cwd=tmp_path))
    assert "binary" in result.block
    assert result.ok


@pytest.mark.asyncio
async def test_build_context_prefix_no_mentions(tmp_path: Path) -> None:
    prefix, resolved = await build_context_prefix("hello world", cwd=tmp_path)
    assert prefix == ""
    assert resolved == []


@pytest.mark.asyncio
async def test_build_context_prefix_injects_file(tmp_path: Path) -> None:
    (tmp_path / "auth.py").write_text("def login(): pass")
    prefix, resolved = await build_context_prefix("Review @auth.py", cwd=tmp_path)
    assert "def login" in prefix
    assert len(resolved) == 1


@pytest.mark.asyncio
async def test_build_context_prefix_budget_exceeded(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("a" * 5_000)
    cfg = InjectionConfig(cwd=tmp_path, mention_token_budget=100, max_file_chars=5_000)
    prefix, _ = await build_context_prefix("@f.py", cwd=tmp_path, cfg=cfg)
    assert "budget exceeded" in prefix or "omitted" in prefix


# ---------------------------------------------------------------------------
# PRD-35 cache: re-mention detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_file_with_cache_unchanged(tmp_path: Path) -> None:
    """Second resolve of unchanged file returns a cached reference block."""
    p = tmp_path / "mod.py"
    p.write_text("x = 1")
    m = Mention(
        raw="@mod.py",
        path="mod.py",
        kind=MentionKind.FILE,
        resolved=p.resolve(),
        start=0,
        end=7,
    )
    cfg = InjectionConfig(cwd=tmp_path)
    cache = MentionCache()

    # First resolve — records in cache
    first = await resolve_mention(m, cfg, cache=cache, current_turn=0)
    assert first.ok
    assert "x = 1" in first.block

    # Second resolve — file unchanged → cached reference
    second = await resolve_mention(m, cfg, cache=cache, current_turn=1)
    assert second.ok
    assert "cached" in second.block
    assert "file unchanged" in second.block


@pytest.mark.asyncio
async def test_resolve_file_with_cache_modified(tmp_path: Path) -> None:
    """Re-resolve of a modified file includes [modified since last mention]."""
    p = tmp_path / "mod.py"
    p.write_text("x = 1")
    m = Mention(
        raw="@mod.py",
        path="mod.py",
        kind=MentionKind.FILE,
        resolved=p.resolve(),
        start=0,
        end=7,
    )
    cfg = InjectionConfig(cwd=tmp_path)
    cache = MentionCache()

    # Record first injection manually so the cache knows about the file
    cache.record("mod.py", p, chars_used=5, turn_index=0)

    # Modify the file
    p.write_text("x = 999")

    # Re-resolve — file has changed
    result = await resolve_mention(m, cfg, cache=cache, current_turn=1)
    assert result.ok
    assert "modified" in result.block
    assert "x = 999" in result.block


# ---------------------------------------------------------------------------
# PRD-35 glob: summary header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_glob_summary_header(tmp_path: Path) -> None:
    """Glob block includes a summary header with arrow (→)."""
    (tmp_path / "a.py").write_text("a = 1")
    (tmp_path / "b.py").write_text("b = 2")
    (tmp_path / "c.py").write_text("c = 3")

    m = Mention(
        raw="@*.py",
        path="*.py",
        kind=MentionKind.GLOB,
        resolved=None,
        start=0,
        end=5,
    )
    cfg = InjectionConfig(cwd=tmp_path, max_glob_files=10)
    result = await resolve_mention(m, cfg)

    assert result.ok
    assert "→" in result.block
    assert "a.py" in result.block or "file" in result.block


# ---------------------------------------------------------------------------
# PRD-35 URL: session cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_url_uses_session_cache(tmp_path: Path) -> None:
    """Second URL resolve returns cached block without fetching again."""
    url_mention = Mention(
        raw="@https://example.com",
        path="https://example.com",
        kind=MentionKind.URL,
        resolved=None,
        start=0,
        end=20,
    )
    cfg = InjectionConfig(cwd=tmp_path, url_timeout_seconds=5.0)
    cache = MentionCache()

    # Pre-populate the URL cache to simulate a prior fetch
    cached_block = '<url href="https://example.com">\nExample Domain\n</url>'
    cache._url_cache["https://example.com"] = cached_block

    # Resolve — should hit cache, no network call
    result = await resolve_mention(url_mention, cfg, cache=cache)
    assert result.ok
    assert result.block == cached_block


@pytest.mark.asyncio
async def test_resolve_url_populates_session_cache(tmp_path: Path) -> None:
    """Fetched URL block is stored in session_url_cache for future calls."""
    url_mention = Mention(
        raw="@https://example.com",
        path="https://example.com",
        kind=MentionKind.URL,
        resolved=None,
        start=0,
        end=20,
    )
    cfg = InjectionConfig(cwd=tmp_path, url_timeout_seconds=5.0)
    cache = MentionCache()

    # Patch _format_url_block to avoid real HTTP
    fake_block = '<url href="https://example.com">\nFetched content\n</url>'

    async def _fake_format(url, timeout, respect_robots=False, session_url_cache=None):
        if session_url_cache is not None:
            session_url_cache[url] = fake_block
        return fake_block

    with patch(
        "agenthicc.mentions.injector._format_url_block",
        side_effect=_fake_format,
    ):
        result = await resolve_mention(url_mention, cfg, cache=cache)

    assert result.ok
    # The block was stored in the cache
    assert cache._url_cache.get("https://example.com") == fake_block


# ---------------------------------------------------------------------------
# build_context_prefix: multiple files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_context_prefix_multiple_files(tmp_path: Path) -> None:
    """Two file mentions are both injected into the prefix."""
    (tmp_path / "a.py").write_text("def a(): pass")
    (tmp_path / "b.py").write_text("def b(): pass")
    prefix, resolved = await build_context_prefix("Compare @a.py and @b.py", cwd=tmp_path)
    assert "def a" in prefix
    assert "def b" in prefix
    assert len(resolved) == 2
    assert all(r.ok for r in resolved)


@pytest.mark.asyncio
async def test_build_context_prefix_with_cache(tmp_path: Path) -> None:
    """Unchanged file gets a cached reference block when cache is passed."""
    p = tmp_path / "stable.py"
    p.write_text("STABLE = True")

    cfg = InjectionConfig(cwd=tmp_path)
    cache = MentionCache()

    # Record initial injection at turn 0
    cache.record("stable.py", p, chars_used=13, turn_index=0)

    # build_context_prefix at turn 1 with cache — file unchanged
    prefix, resolved = await build_context_prefix(
        "Again @stable.py",
        cwd=tmp_path,
        cfg=cfg,
        cache=cache,
        current_turn=1,
    )
    assert len(resolved) == 1
    assert "cached" in resolved[0].block or "unchanged" in resolved[0].block


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_file_missing_returns_error(tmp_path: Path) -> None:
    """A FILE mention whose path disappeared returns an error block."""
    p = tmp_path / "vanished.py"
    # Create then delete
    p.write_text("oops")
    m = Mention(
        raw="@vanished.py",
        path="vanished.py",
        kind=MentionKind.FILE,
        resolved=p.resolve(),
        start=0,
        end=12,
    )
    p.unlink()
    cfg = InjectionConfig(cwd=tmp_path)
    result = await resolve_mention(m, cfg)
    assert not result.ok


@pytest.mark.asyncio
async def test_resolve_glob_no_matches(tmp_path: Path) -> None:
    """Glob with no matching files returns a warning block."""
    m = Mention(
        raw="@*.xyz",
        path="*.xyz",
        kind=MentionKind.GLOB,
        resolved=None,
        start=0,
        end=6,
    )
    cfg = InjectionConfig(cwd=tmp_path)
    result = await resolve_mention(m, cfg)
    assert "no" in result.block.lower() or "matched" in result.block.lower()


@pytest.mark.asyncio
async def test_build_context_prefix_unresolved_included_as_warning(tmp_path: Path) -> None:
    """Unresolved mentions are included in the prefix as warning lines."""
    prefix, resolved = await build_context_prefix("Check @ghost.py", cwd=tmp_path)
    assert len(resolved) == 1
    assert resolved[0].error == "not_found"
    # Warning still appears in prefix
    assert "not found" in prefix or "ghost.py" in prefix


@pytest.mark.asyncio
async def test_injection_config_defaults() -> None:
    """InjectionConfig defaults are sane."""
    cfg = InjectionConfig()
    assert cfg.mention_token_budget == 32_000
    assert cfg.max_file_chars == 16_000
    assert cfg.max_glob_files == 20
    assert cfg.url_timeout_seconds == 10.0


def test_injected_content_ok_property(tmp_path: Path) -> None:
    """InjectedContent.ok reflects whether error is None."""
    m = _unresolved("x.py")
    good = InjectedContent(mention=m, block="content", chars_used=7, error=None)
    bad = InjectedContent(mention=m, block="", chars_used=0, error="not_found")
    assert good.ok is True
    assert bad.ok is False
