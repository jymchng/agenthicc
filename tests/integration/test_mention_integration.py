"""Integration tests for the @mention injection pipeline (PRD-33/35).

Tests cover the full path from text parsing through content resolution,
token budget enforcement, cache behaviour, and TranscriptModel chip rendering.
"""

from __future__ import annotations

import pytest
from pathlib import Path

from agenthicc.mentions.injector import build_context_prefix, InjectionConfig
from agenthicc.mentions.cache import MentionCache
from agenthicc.mentions.parser import parse_mentions, MentionKind
from agenthicc.tui.transcript import TranscriptModel, MentionChip

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 1. Single file — full pipeline
# ---------------------------------------------------------------------------


async def test_full_pipeline_single_file(tmp_path):
    """build_context_prefix injects a single file's contents into the prefix."""
    f = tmp_path / "hello.py"
    f.write_text("def hello(): return 42\n", encoding="utf-8")

    prefix, injected = await build_context_prefix(f"Review @hello.py", cwd=tmp_path)

    assert prefix, "expected a non-empty prefix"
    assert "def hello" in prefix
    assert "hello.py" in prefix
    assert len(injected) == 1
    assert injected[0].ok
    assert injected[0].mention.kind == MentionKind.FILE


# ---------------------------------------------------------------------------
# 2. Multiple files — all injected
# ---------------------------------------------------------------------------


async def test_full_pipeline_multiple_files(tmp_path):
    """Three file mentions are all resolved and injected."""
    (tmp_path / "a.py").write_text("A = 1", encoding="utf-8")
    (tmp_path / "b.py").write_text("B = 2", encoding="utf-8")
    (tmp_path / "c.py").write_text("C = 3", encoding="utf-8")

    prefix, injected = await build_context_prefix(
        "Check @a.py @b.py @c.py", cwd=tmp_path
    )

    assert "A = 1" in prefix
    assert "B = 2" in prefix
    assert "C = 3" in prefix
    assert len(injected) == 3
    assert all(r.ok for r in injected)


# ---------------------------------------------------------------------------
# 3. Unresolved mention — warning in prefix, pipeline not blocked
# ---------------------------------------------------------------------------


async def test_full_pipeline_unresolved(tmp_path):
    """An @mention pointing to a non-existent file results in a warning but
    does NOT raise and does NOT block other mentions."""
    (tmp_path / "real.py").write_text("REAL = True", encoding="utf-8")

    prefix, injected = await build_context_prefix(
        "Fix @ghost.txt and @real.py", cwd=tmp_path
    )

    # Pipeline still runs; prefix is not empty
    assert "REAL = True" in prefix or "real.py" in prefix
    # Warning included for unresolved mention
    assert "ghost.txt" in prefix or any(r.error for r in injected if "ghost" in r.mention.raw)
    # Two mentions parsed
    assert len(injected) == 2
    ghost = next(r for r in injected if "ghost" in r.mention.raw)
    assert not ghost.ok
    assert ghost.error == "not_found"


# ---------------------------------------------------------------------------
# 4. Glob pattern — all matching files included in prefix
# ---------------------------------------------------------------------------


async def test_full_pipeline_glob(tmp_path):
    """@*.py glob expands to all Python files in the directory."""
    for name in ["mod_a.py", "mod_b.py", "mod_c.py", "mod_d.py", "mod_e.py"]:
        (tmp_path / name).write_text(f"# {name}", encoding="utf-8")

    prefix, injected = await build_context_prefix(f"@*.py", cwd=tmp_path)

    assert len(injected) == 1, "one glob mention"
    r = injected[0]
    assert r.mention.kind == MentionKind.GLOB
    # All 5 files should appear in the block
    for name in ["mod_a.py", "mod_b.py", "mod_c.py", "mod_d.py", "mod_e.py"]:
        assert name in prefix, f"{name} should be in prefix"


# ---------------------------------------------------------------------------
# 5. Glob with budget — files omitted when budget exceeded
# ---------------------------------------------------------------------------


async def test_full_pipeline_glob_budget(tmp_path):
    """When 10 files × 200 chars each exceed a small budget, some are omitted."""
    for i in range(10):
        (tmp_path / f"f{i}.py").write_text("x" * 200, encoding="utf-8")

    cfg = InjectionConfig(cwd=tmp_path, mention_token_budget=500, max_glob_files=20)
    prefix, injected = await build_context_prefix("@*.py", cwd=tmp_path, cfg=cfg)

    assert len(injected) == 1
    r = injected[0]
    # Not all 10 files fit — the block should report omissions
    file_count = r.block.count("<file ")
    assert file_count < 10, f"Expected fewer than 10 files injected, got {file_count}"


# ---------------------------------------------------------------------------
# 6. Cache — prevents re-injection of unchanged file
# ---------------------------------------------------------------------------


async def test_cache_prevents_reinjection(tmp_path):
    """Second call for unchanged file returns cached reference instead of content."""
    f = tmp_path / "stable.py"
    f.write_text("STABLE = True", encoding="utf-8")

    cache = MentionCache()
    cfg = InjectionConfig(cwd=tmp_path)

    # First call — file is new, full content injected
    prefix1, injected1 = await build_context_prefix(
        "@stable.py", cwd=tmp_path, cfg=cfg, cache=cache, current_turn=0
    )
    assert "STABLE = True" in prefix1

    # Second call — file unchanged, cached reference returned
    prefix2, injected2 = await build_context_prefix(
        "@stable.py", cwd=tmp_path, cfg=cfg, cache=cache, current_turn=1
    )
    assert len(injected2) == 1
    assert "cached" in injected2[0].block or "unchanged" in injected2[0].block.lower()


# ---------------------------------------------------------------------------
# 7. Cache — detects file modification
# ---------------------------------------------------------------------------


async def test_cache_detects_modification(tmp_path):
    """When a file changes between turns the second injection shows a 'modified' note."""
    f = tmp_path / "changing.py"
    f.write_text("VERSION = 1", encoding="utf-8")

    cache = MentionCache()
    cfg = InjectionConfig(cwd=tmp_path)

    await build_context_prefix(
        "@changing.py", cwd=tmp_path, cfg=cfg, cache=cache, current_turn=0
    )

    # Modify the file
    f.write_text("VERSION = 2", encoding="utf-8")

    prefix2, injected2 = await build_context_prefix(
        "@changing.py", cwd=tmp_path, cfg=cfg, cache=cache, current_turn=1
    )

    assert len(injected2) == 1
    r = injected2[0]
    assert r.ok
    assert "modified" in r.block.lower() or "VERSION = 2" in r.block


# ---------------------------------------------------------------------------
# 8. TranscriptModel — chips added after injection
# ---------------------------------------------------------------------------


async def test_transcript_chips_added_after_injection(tmp_path):
    """add_mention_chips puts chip entries in the transcript that appear in render()."""
    f = tmp_path / "util.py"
    f.write_text("def util(): pass", encoding="utf-8")

    _, injected = await build_context_prefix("@util.py", cwd=tmp_path)

    model = TranscriptModel()
    model.append_turn("agent-1", "assistant", 0.0)

    for r in injected:
        chip = MentionChip(
            raw=r.mention.raw,
            kind="file",
            display_size=f"{r.chars_used / 1024:.1f} KB",
            ok=r.ok,
        )
        model.add_mention_chips("agent-1", [chip])

    lines = model.render()
    chip_line = next((l for l in lines if "@util.py" in l), None)
    assert chip_line is not None, "chip line should appear in render output"
    assert "✓" in chip_line


# ---------------------------------------------------------------------------
# 9. Expand chip — set expanded=True, render shows content lines
# ---------------------------------------------------------------------------


async def test_expand_chip_in_transcript(tmp_path):
    """When chip.expanded is True the mention content lines appear in render."""
    f = tmp_path / "expand_me.py"
    f.write_text("LINE_1 = 'one'\nLINE_2 = 'two'\n", encoding="utf-8")

    prefix, injected = await build_context_prefix("@expand_me.py", cwd=tmp_path)

    model = TranscriptModel()
    model.append_turn("a1", "assistant", 0.0)

    r = injected[0]
    chip = MentionChip(raw=r.mention.raw, kind="file", display_size="", ok=r.ok, expanded=True)
    model.add_mention_chips("a1", [chip])
    model.set_mention_content("a1", r.mention.raw, r.block)

    lines = model.render()
    # The content block should appear as indented dim lines
    content_lines = [l for l in lines if "LINE_1" in l or "LINE_2" in l]
    assert content_lines, "expanded content should appear in render output"


# ---------------------------------------------------------------------------
# 10. Unresolved chip — ok=False and error rendered
# ---------------------------------------------------------------------------


def test_mention_chip_unresolved_shows_error():
    """An unresolved mention chip renders with ✗ and error text."""
    model = TranscriptModel()
    model.append_turn("a1", "assistant", 0.0)
    chip = MentionChip(
        raw="@missing.txt",
        kind="unresolved",
        display_size="",
        ok=False,
        error="not found",
    )
    model.add_mention_chips("a1", [chip])

    lines = model.render()
    chip_line = next((l for l in lines if "@missing.txt" in l), None)
    assert chip_line is not None
    assert "✗" in chip_line
    assert "not found" in chip_line


# ---------------------------------------------------------------------------
# 11. Binary file — placeholder not raw bytes
# ---------------------------------------------------------------------------


async def test_binary_file_gets_placeholder(tmp_path):
    """A PNG file receives a binary placeholder block, not raw content."""
    img = tmp_path / "icon.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    prefix, injected = await build_context_prefix("@icon.png", cwd=tmp_path)

    assert len(injected) == 1
    r = injected[0]
    assert r.ok
    assert "binary" in r.block


# ---------------------------------------------------------------------------
# 12. Large file — truncated with note in block
# ---------------------------------------------------------------------------


async def test_large_file_truncated(tmp_path):
    """A file larger than max_file_chars is truncated with a marker."""
    big = tmp_path / "big.py"
    big.write_text("A" * 50_000, encoding="utf-8")

    cfg = InjectionConfig(cwd=tmp_path, max_file_chars=200)
    prefix, injected = await build_context_prefix("@big.py", cwd=tmp_path, cfg=cfg)

    assert len(injected) == 1
    r = injected[0]
    assert "truncated" in r.block


# ---------------------------------------------------------------------------
# 13. Budget exceeded — last mention omitted with message
# ---------------------------------------------------------------------------


async def test_budget_exceeded_omits_last(tmp_path):
    """When the overall budget is exhausted the last mention is omitted or truncated."""
    (tmp_path / "small.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "large.py").write_text("Y" * 5_000, encoding="utf-8")

    cfg = InjectionConfig(cwd=tmp_path, mention_token_budget=50, max_file_chars=5_000)
    prefix, injected = await build_context_prefix(
        "@small.py @large.py", cwd=tmp_path, cfg=cfg
    )

    # Budget already used by small.py; large.py should be omitted/truncated
    assert "budget exceeded" in prefix or "omitted" in prefix


# ---------------------------------------------------------------------------
# 14. Directory mention — listing block in prefix
# ---------------------------------------------------------------------------


async def test_injector_with_directory(tmp_path):
    """@dir/ resolves to a directory listing block."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "alpha.py").write_text("ALPHA = 1")
    (src / "beta.py").write_text("BETA = 2")

    prefix, injected = await build_context_prefix("@src/", cwd=tmp_path)

    assert len(injected) == 1
    r = injected[0]
    assert r.mention.kind == MentionKind.DIRECTORY
    assert r.ok
    assert "<dir" in r.block
    assert "alpha.py" in r.block or "beta.py" in r.block


# ---------------------------------------------------------------------------
# 15. No mentions — prefix is empty string
# ---------------------------------------------------------------------------


async def test_pipeline_no_mentions(tmp_path):
    """Text without any @mentions returns an empty prefix and empty resolved list."""
    prefix, injected = await build_context_prefix(
        "Hello, just a regular message with no at signs", cwd=tmp_path
    )

    assert prefix == ""
    assert injected == []
