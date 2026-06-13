"""Tests for @mention UI — transcript chips, autocomplete polish (PRD-34)."""
from __future__ import annotations

import pytest
from pathlib import Path

from agenthicc.tui.transcript import TranscriptModel, MentionChip

pytestmark = pytest.mark.unit


# ── MentionChip dataclass ──────────────────────────────────────────────────


def test_mention_chip_defaults():
    chip = MentionChip(raw="@foo.py", kind="file", display_size="1 KB", ok=True)
    assert chip.error is None
    assert chip.expanded is False


def test_mention_chip_error_fields():
    chip = MentionChip(raw="@ghost.txt", kind="unresolved", display_size="", ok=False, error="not found")
    assert chip.ok is False
    assert chip.error == "not found"


# ── PRD-34 Tests section ───────────────────────────────────────────────────


def test_mention_chip_ok_renders_green():
    m = TranscriptModel()
    m.append_turn("a1", "assistant", 0.0)
    m.add_mention_chips("a1", [
        MentionChip(raw="@auth.py", kind="file", display_size="1.2 KB", ok=True)
    ])
    lines = m.render()
    chip_line = next((l for l in lines if "@auth.py" in l), None)
    assert chip_line is not None
    assert "✓" in chip_line


def test_mention_chip_error_renders_red():
    m = TranscriptModel()
    m.append_turn("a1", "assistant", 0.0)
    m.add_mention_chips("a1", [
        MentionChip(raw="@ghost.txt", kind="unresolved", display_size="",
                    ok=False, error="not found")
    ])
    lines = m.render()
    chip_line = next((l for l in lines if "@ghost.txt" in l), None)
    assert chip_line is not None
    assert "✗" in chip_line
    assert "not found" in chip_line


def test_multiple_chips_all_rendered():
    m = TranscriptModel()
    m.append_turn("a1", "agent", 0.0)
    chips = [
        MentionChip(raw="@a.py", kind="file", display_size="1 KB", ok=True),
        MentionChip(raw="@b.py", kind="file", display_size="2 KB", ok=True),
    ]
    m.add_mention_chips("a1", chips)
    lines = m.render()
    assert any("@a.py" in l for l in lines)
    assert any("@b.py" in l for l in lines)


def test_completer_display_meta_has_size(tmp_path):
    from agenthicc.tui.input_bar import AtMentionCompleter

    (tmp_path / "big.py").write_bytes(b"x" * 2048)
    completer = AtMentionCompleter(base_path=tmp_path)
    results = completer.completions("big")
    assert results
    _, meta = results[0]
    assert meta  # non-empty size string


# ── Additional tests ───────────────────────────────────────────────────────


def test_add_mention_chips_stores_on_turn():
    m = TranscriptModel()
    m.append_turn("a1", "agent", 0.0)
    chip = MentionChip(raw="@readme.md", kind="file", display_size="5 KB", ok=True)
    m.add_mention_chips("a1", [chip])
    turn = m.turns[-1]
    assert hasattr(turn, "mention_chips")
    assert len(turn.mention_chips) == 1
    assert turn.mention_chips[0].raw == "@readme.md"


def test_expand_chip_shows_content():
    m = TranscriptModel()
    m.append_turn("a1", "agent", 0.0)
    chip = MentionChip(raw="@src/config.py", kind="file", display_size="2 KB", ok=True)
    m.add_mention_chips("a1", [chip])
    m.set_mention_content("a1", "@src/config.py", "line one\nline two\nline three")
    # Before expanding: content lines not rendered
    lines_before = m.render()
    assert not any("line one" in l for l in lines_before)
    # After expanding
    chip.expanded = True
    lines_after = m.render()
    assert any("line one" in l for l in lines_after)
    assert any("line two" in l for l in lines_after)


def test_expand_chip_truncates_at_50_lines():
    m = TranscriptModel()
    m.append_turn("a1", "agent", 0.0)
    chip = MentionChip(raw="@big.py", kind="file", display_size="10 KB", ok=True, expanded=True)
    m.add_mention_chips("a1", [chip])
    content = "\n".join(f"line {i}" for i in range(60))
    m.set_mention_content("a1", "@big.py", content)
    lines = m.render()
    # Should have truncation note
    assert any("more lines" in l for l in lines)


def test_chip_no_meta_when_display_size_empty():
    m = TranscriptModel()
    m.append_turn("a1", "agent", 0.0)
    chip = MentionChip(raw="@empty.txt", kind="file", display_size="", ok=True)
    m.add_mention_chips("a1", [chip])
    lines = m.render()
    chip_line = next((l for l in lines if "@empty.txt" in l), None)
    assert chip_line is not None
    assert "✓" in chip_line
    # No trailing [dim]...[/dim] size block
    assert "[dim][/dim]" not in chip_line


def test_entry_meta_file_size(tmp_path):
    from agenthicc.tui.input_bar import _entry_meta
    f = tmp_path / "hello.py"
    f.write_bytes(b"x" * 512)
    result = _entry_meta(f)
    assert "B" in result or "KB" in result


def test_entry_meta_file_kilobytes(tmp_path):
    from agenthicc.tui.input_bar import _entry_meta
    f = tmp_path / "medium.py"
    f.write_bytes(b"x" * 2048)
    result = _entry_meta(f)
    assert "KB" in result


def test_entry_meta_directory(tmp_path):
    from agenthicc.tui.input_bar import _entry_meta
    d = tmp_path / "mydir"
    d.mkdir()
    result = _entry_meta(d)
    assert result == "dir"


def test_entry_meta_python_file(tmp_path):
    from agenthicc.tui.input_bar import _entry_meta
    f = tmp_path / "script.py"
    f.write_bytes(b"x" * 2048)
    assert "KB" in _entry_meta(f)


def test_entry_meta_directory(tmp_path):
    from agenthicc.tui.input_bar import _entry_meta
    d = tmp_path / "subdir"
    d.mkdir()
    assert _entry_meta(d) == "dir"


def test_entry_meta_small_file(tmp_path):
    from agenthicc.tui.input_bar import _entry_meta
    f = tmp_path / "tiny.txt"
    f.write_bytes(b"hi")
    assert "B" in _entry_meta(f)


def test_completer_url_completion():
    from agenthicc.tui.input_bar import AtMentionCompleter

    urls = ["https://example.com/page", "https://docs.python.org/3/"]
    completer = AtMentionCompleter(recent_urls=urls)
    results = completer.completions("https://example")
    assert len(results) == 1
    path, meta = results[0]
    assert "example.com" in path
    assert meta == "url"


def test_completer_url_no_match():
    from agenthicc.tui.input_bar import AtMentionCompleter

    urls = ["https://example.com/page"]
    completer = AtMentionCompleter(recent_urls=urls)
    results = completer.completions("https://other")
    assert len(results) == 0


def test_completer_url_partial_prefix_yields_match():
    """A partial URL prefix returns the matching URL."""
    from agenthicc.tui.input_bar import AtMentionCompleter

    url = "https://example.com/page"
    completer = AtMentionCompleter(recent_urls=[url])
    results = completer.completions("https://example")
    assert len(results) == 1
    path, meta = results[0]
    assert path == url
    assert meta == "url"


def test_completer_display_uses_plus_prefix(tmp_path):
    from agenthicc.tui.input_bar import AtMentionCompleter

    (tmp_path / "app.py").write_text("pass")
    completer = AtMentionCompleter(base_path=tmp_path)
    results = completer.completions("app")
    assert results
    path, _ = results[0]
    assert "app.py" in path


def test_expand_slash_command_mention_chip():
    """SlashCommandHandler._expand toggles mention chips when prefix starts with @."""
    from unittest.mock import MagicMock
    from agenthicc.tui.app import SlashCommandHandler

    m = TranscriptModel()
    m.append_turn("a1", "agent", 0.0)
    chip = MentionChip(raw="@src/auth.py", kind="file", display_size="3 KB", ok=True)
    m.add_mention_chips("a1", [chip])

    console = MagicMock()
    handler = SlashCommandHandler()
    handler._expand("/expand @src/auth.py", m, console)

    assert chip.expanded is True
    console.print.assert_called_once()
    call_arg = console.print.call_args[0][0]
    assert "Expanded" in call_arg


def test_expand_slash_command_no_match():
    from unittest.mock import MagicMock
    from agenthicc.tui.app import SlashCommandHandler

    m = TranscriptModel()
    console = MagicMock()
    handler = SlashCommandHandler()
    handler._expand("/expand @nonexistent.py", m, console)

    call_arg = console.print.call_args[0][0]
    assert "No item found" in call_arg


def test_add_mention_chips_multiple_calls_accumulate():
    m = TranscriptModel()
    m.append_turn("a1", "agent", 0.0)
    m.add_mention_chips("a1", [MentionChip(raw="@a.py", kind="file", display_size="1 KB", ok=True)])
    m.add_mention_chips("a1", [MentionChip(raw="@b.py", kind="file", display_size="2 KB", ok=True)])
    turn = m.turns[-1]
    assert len(turn.mention_chips) == 2


def test_set_mention_content_overwrite():
    m = TranscriptModel()
    m.append_turn("a1", "agent", 0.0)
    m.set_mention_content("a1", "@x.py", "first content")
    m.set_mention_content("a1", "@x.py", "second content")
    turn = m.turns[-1]
    assert turn.mention_content["@x.py"] == "second content"
