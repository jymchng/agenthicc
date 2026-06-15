"""Unit tests for input_bar completers (no prompt_toolkit)."""
from __future__ import annotations

import pytest
from pathlib import Path

from agenthicc.tui.input.completions import (
    AtMentionCompleter,
    BUILTIN_COMMANDS,
    CommandSpec,
    SlashCommandCompleter,
    _entry_meta,
)

pytestmark = pytest.mark.unit


class TestSlashCommandCompleter:
    def test_completes_partial(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        results = comp.matches("/sta")
        assert any("status" in c.name for c in results)

    def test_all_on_slash_alone(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        results = comp.matches("/")
        assert len(results) >= len(BUILTIN_COMMANDS)

    def test_no_match_without_slash(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        assert comp.matches("hello") == []

    def test_unknown_prefix(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        assert comp.matches("/zzz") == []

    def test_slash_mid_sentence(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        results = comp.get_match_for_line("hello /sta")
        assert any("status" in c.name for c in results)

    def test_register_dynamic_command(self):
        comp = SlashCommandCompleter(list(BUILTIN_COMMANDS))
        comp.add(CommandSpec("/mycommand", "A test command"))
        results = comp.matches("/myc")
        assert any("mycommand" in c.name for c in results)

    def test_description_available(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        results = comp.matches("/s")
        assert all(c.description for c in results)


class TestAtMentionCompleter:
    def test_completes_file_after_at(self, tmp_path):
        (tmp_path / "auth.py").write_text("x")
        (tmp_path / "hashing.py").write_text("x")
        comp = AtMentionCompleter(base_path=tmp_path)
        results = comp.completions("au")
        names = [p for p, _ in results]
        assert any("auth.py" in n for n in names)

    def test_empty_fragment_lists_all_non_hidden(self, tmp_path):
        for name in ["alpha.py", "beta.py", ".hidden"]:
            (tmp_path / name).write_text("x")
        comp = AtMentionCompleter(base_path=tmp_path)
        results = comp.completions("")
        names = [p for p, _ in results]
        assert any("alpha.py" in n for n in names)
        assert any("beta.py" in n for n in names)
        assert not any(".hidden" in n for n in names)

    def test_subdir(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "login.py").write_text("x")
        comp = AtMentionCompleter(base_path=tmp_path)
        results = comp.completions("src/log")
        assert len(results) >= 1

    def test_dirs_have_trailing_slash(self, tmp_path):
        (tmp_path / "src").mkdir()
        comp = AtMentionCompleter(base_path=tmp_path)
        results = comp.completions("")
        names = [p for p, _ in results]
        assert any("src/" in n for n in names)

    def test_nonexistent_subdir_returns_empty(self, tmp_path):
        comp = AtMentionCompleter(base_path=tmp_path)
        assert comp.completions("nonexistent/") == []

    def test_meta_dir(self, tmp_path):
        d = tmp_path / "subdir"
        d.mkdir()
        assert _entry_meta(d) == "dir"

    def test_meta_file_size(self, tmp_path):
        f = tmp_path / "big.py"
        f.write_bytes(b"x" * 2048)
        assert "KB" in _entry_meta(f)
