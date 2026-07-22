"""E2E tests for @mention injection — verifies content reaches the agent via AgentRunnerBase.

These tests exercise build_context_prefix() directly and verify the resulting
prefix + text that would be passed to _active_runner.run().  No real LLM calls
are made; all tests are self-contained with tmp_path fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.mentions.cache import MentionCache
from agenthicc.mentions.injector import InjectionConfig, build_context_prefix

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# 1. File content injected into agent text
# ---------------------------------------------------------------------------


async def test_file_content_injected_into_agent_text(tmp_path: Path) -> None:
    """File content appears in the prefix before the user message."""
    py_file = tmp_path / "module.py"
    py_file.write_text("def greet():\n    return 'hello'\n")

    cfg = InjectionConfig(cwd=tmp_path)
    prefix, resolved = await build_context_prefix("explain @module.py", cwd=tmp_path, cfg=cfg)

    assert prefix != "", "prefix must not be empty for a resolved file"
    assert "<file" in prefix
    assert "def greet" in prefix
    assert "return 'hello'" in prefix

    agent_text = prefix + "explain @module.py"
    assert "def greet" in agent_text
    assert "explain @module.py" in agent_text
    # Prefix comes before the user message
    assert agent_text.index("<file") < agent_text.index("explain @module.py")

    assert len(resolved) == 1
    assert resolved[0].ok is True
    assert resolved[0].error is None


# ---------------------------------------------------------------------------
# 2. Unresolved mention produces a warning block, not a crash
# ---------------------------------------------------------------------------


async def test_unresolved_mention_warning_in_text(tmp_path: Path) -> None:
    """@nonexistent.txt produces a warning block; the original message is preserved."""
    cfg = InjectionConfig(cwd=tmp_path)
    prefix, resolved = await build_context_prefix(
        "look at @nonexistent.txt please", cwd=tmp_path, cfg=cfg
    )

    assert prefix != ""
    assert "nonexistent.txt" in prefix
    assert "not found" in prefix or "⚠" in prefix

    assert len(resolved) == 1
    assert resolved[0].ok is False
    assert resolved[0].error == "not_found"

    agent_text = prefix + "look at @nonexistent.txt please"
    assert "look at @nonexistent.txt please" in agent_text


# ---------------------------------------------------------------------------
# 3. Multiple file mentions all appear in the prefix
# ---------------------------------------------------------------------------


async def test_multiple_files_all_in_prefix(tmp_path: Path) -> None:
    """Both @auth.py and @config.py have their content in the prefix."""
    (tmp_path / "auth.py").write_text("AUTH_SECRET = 'abc123'\n")
    (tmp_path / "config.py").write_text("DEBUG = False\nHOST = 'localhost'\n")

    cfg = InjectionConfig(cwd=tmp_path)
    prefix, resolved = await build_context_prefix(
        "Review @auth.py and @config.py", cwd=tmp_path, cfg=cfg
    )

    assert "AUTH_SECRET" in prefix
    assert "DEBUG = False" in prefix
    assert prefix.count("<file") == 2

    assert len(resolved) == 2
    assert all(r.ok for r in resolved)


# ---------------------------------------------------------------------------
# 4. Directory mention produces a <dir> block
# ---------------------------------------------------------------------------


async def test_directory_listing_in_prefix(tmp_path: Path) -> None:
    """@src/ injects a <dir> block listing all files."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "models.py").write_text("class User: ...")
    (src / "views.py").write_text("def index(): ...")
    (src / "utils.py").write_text("def helper(): ...")

    cfg = InjectionConfig(cwd=tmp_path)
    prefix, resolved = await build_context_prefix("look at @src/", cwd=tmp_path, cfg=cfg)

    assert "<dir" in prefix
    assert "models.py" in prefix
    assert "views.py" in prefix
    assert "utils.py" in prefix

    assert len(resolved) == 1
    assert resolved[0].ok is True
    assert resolved[0].mention.path == "src/"


# ---------------------------------------------------------------------------
# 5. Glob mention includes all matched files with a summary header
# ---------------------------------------------------------------------------


async def test_glob_files_in_prefix(tmp_path: Path) -> None:
    """@*.py injects all 3 Python files with a <!-- summary --> header."""
    (tmp_path / "alpha.py").write_text("ALPHA = 1\n")
    (tmp_path / "beta.py").write_text("BETA = 2\n")
    (tmp_path / "gamma.py").write_text("GAMMA = 3\n")

    cfg = InjectionConfig(cwd=tmp_path)
    prefix, resolved = await build_context_prefix("check @*.py", cwd=tmp_path, cfg=cfg)

    assert "<!--" in prefix, "glob should produce a summary comment header"
    assert "3 file" in prefix
    assert "ALPHA = 1" in prefix
    assert "BETA = 2" in prefix
    assert "GAMMA = 3" in prefix
    assert prefix.count("<file") == 3

    assert len(resolved) == 1
    assert resolved[0].ok is True
    assert resolved[0].mention.kind.value == "glob"


# ---------------------------------------------------------------------------
# 6. Binary file gets a placeholder, not raw bytes
# ---------------------------------------------------------------------------


async def test_binary_file_gets_placeholder(tmp_path: Path) -> None:
    """A PNG file injected via @mention gets a binary placeholder block."""
    png = tmp_path / "image.png"
    # Minimal PNG header with null bytes to trigger binary detection
    png.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01")

    cfg = InjectionConfig(cwd=tmp_path)
    prefix, resolved = await build_context_prefix("what is @image.png", cwd=tmp_path, cfg=cfg)

    assert "binary" in prefix
    # Raw null bytes must NOT appear in the prefix
    assert "\x00" not in prefix
    assert "image.png" in prefix

    assert len(resolved) == 1
    assert resolved[0].ok is True


# ---------------------------------------------------------------------------
# 7. Large file is truncated at max_file_chars
# ---------------------------------------------------------------------------


async def test_large_file_truncated(tmp_path: Path) -> None:
    """Files exceeding max_file_chars are truncated with a marker."""
    big = tmp_path / "huge.txt"
    big.write_text("x" * 50_000)

    cfg = InjectionConfig(cwd=tmp_path, max_file_chars=100)
    prefix, resolved = await build_context_prefix("@huge.txt", cwd=tmp_path, cfg=cfg)

    assert "truncated" in prefix
    assert len(prefix) < 50_000 + 200  # must be much shorter than the raw file
    assert "x" * 100 in prefix  # first 100 chars should be present

    assert len(resolved) == 1
    assert resolved[0].ok is True


# ---------------------------------------------------------------------------
# 8. No @mention → empty prefix
# ---------------------------------------------------------------------------


async def test_no_mention_prefix_is_empty(tmp_path: Path) -> None:
    """A plain message with no @mentions returns an empty prefix."""
    cfg = InjectionConfig(cwd=tmp_path)
    prefix, resolved = await build_context_prefix("just a normal message", cwd=tmp_path, cfg=cfg)

    assert prefix == ""
    assert resolved == []


# ---------------------------------------------------------------------------
# 9. Cache returns a reference on second identical mention (unchanged file)
# ---------------------------------------------------------------------------


async def test_cache_returns_reference_on_second_mention(tmp_path: Path) -> None:
    """Second injection of an unchanged file produces a 'Same content as turn N' block."""
    f = tmp_path / "shared.py"
    f.write_text("VALUE = 99\n")

    cache = MentionCache()
    cfg = InjectionConfig(cwd=tmp_path)

    prefix1, _ = await build_context_prefix(
        "@shared.py", cwd=tmp_path, cfg=cfg, cache=cache, current_turn=0
    )
    assert "VALUE = 99" in prefix1
    assert "cached" not in prefix1

    prefix2, resolved2 = await build_context_prefix(
        "@shared.py", cwd=tmp_path, cfg=cfg, cache=cache, current_turn=1
    )
    assert "Same content as turn 0" in prefix2
    assert 'cached="true"' in prefix2
    # The raw file content should NOT be repeated
    assert "VALUE = 99" not in prefix2

    assert len(resolved2) == 1
    assert resolved2[0].ok is True


# ---------------------------------------------------------------------------
# 10. Cache detects a modified file and includes a "modified since" prefix
# ---------------------------------------------------------------------------


async def test_cache_detects_modified_file(tmp_path: Path) -> None:
    """After the file changes, the second injection notes it was modified."""
    f = tmp_path / "data.py"
    f.write_text("VERSION = 1\n")

    cache = MentionCache()
    cfg = InjectionConfig(cwd=tmp_path)

    await build_context_prefix("@data.py", cwd=tmp_path, cfg=cfg, cache=cache, current_turn=0)

    # Modify the file
    f.write_text("VERSION = 2\n")

    prefix2, resolved2 = await build_context_prefix(
        "@data.py", cwd=tmp_path, cfg=cfg, cache=cache, current_turn=1
    )

    assert "modified since last mention" in prefix2
    assert "VERSION = 2" in prefix2

    assert resolved2[0].ok is True
