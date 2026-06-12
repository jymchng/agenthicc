"""Unit tests for InputBarSession, completers, and key bindings (PRD-10)."""
from __future__ import annotations

import pytest
from pathlib import Path
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from agenthicc.tui.input_bar import (
    AtMentionCompleter,
    BUILTIN_COMMANDS,
    CommandSpec,
    InputBarSession,
    SlashCommandCompleter,
)

pytestmark = pytest.mark.unit

CE = CompleteEvent()


class TestSlashCommandCompleter:
    def test_completes_partial(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        results = list(comp.get_completions(Document("/sta"), CE))
        full = ["/sta" + r.text for r in results]
        assert any("status" in t for t in full)

    def test_all_on_slash_alone(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        results = list(comp.get_completions(Document("/"), CE))
        assert len(results) >= len(BUILTIN_COMMANDS)

    def test_no_completion_without_slash(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        assert list(comp.get_completions(Document("hello"), CE)) == []

    def test_no_completion_empty(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        assert list(comp.get_completions(Document(""), CE)) == []

    def test_unknown_prefix(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        assert list(comp.get_completions(Document("/zzz"), CE)) == []

    def test_description_in_meta(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        results = list(comp.get_completions(Document("/s"), CE))
        assert any(r.display_meta for r in results)

    def test_slash_mid_sentence(self):
        comp = SlashCommandCompleter(BUILTIN_COMMANDS)
        results = list(comp.get_completions(Document("hello /sta"), CE))
        full = ["/sta" + r.text for r in results]
        assert any("status" in t for t in full)

    def test_register_dynamic_command(self):
        comp = SlashCommandCompleter(list(BUILTIN_COMMANDS))
        comp.add(CommandSpec("/mycommand", "A test command"))
        results = list(comp.get_completions(Document("/myc"), CE))
        full = ["/myc" + r.text for r in results]
        assert any("mycommand" in t for t in full)


class TestAtMentionCompleter:
    def test_completes_file_after_at(self, tmp_path):
        (tmp_path / "auth.py").write_text("x")
        (tmp_path / "hashing.py").write_text("x")
        comp = AtMentionCompleter(base_path=tmp_path)
        results = list(comp.get_completions(Document("fix bug in @au"), CE))
        full_names = ["au" + r.text for r in results]
        assert any("auth.py" in n for n in full_names)

    def test_no_completion_without_at(self, tmp_path):
        (tmp_path / "main.py").write_text("x")
        comp = AtMentionCompleter(base_path=tmp_path)
        results = list(comp.get_completions(Document("fix main.py"), CE))
        assert results == []

    def test_empty_fragment_lists_all_non_hidden(self, tmp_path):
        for name in ["alpha.py", "beta.py", ".hidden"]:
            (tmp_path / name).write_text("x")
        comp = AtMentionCompleter(base_path=tmp_path)
        results = list(comp.get_completions(Document("@"), CE))
        names = [r.display for r in results]
        assert any("alpha.py" in str(n) for n in names)
        assert any("beta.py" in str(n) for n in names)
        assert not any(".hidden" in str(n) for n in names)

    def test_subdir(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "login.py").write_text("x")
        (src / "logout.py").write_text("x")
        comp = AtMentionCompleter(base_path=tmp_path)
        results = list(comp.get_completions(Document("@src/log"), CE))
        assert len(results) >= 1

    def test_multiple_at_last_one(self, tmp_path):
        (tmp_path / "alpha.py").write_text("x")
        (tmp_path / "beta.py").write_text("x")
        comp = AtMentionCompleter(base_path=tmp_path)
        results = list(comp.get_completions(Document("compare @alpha.py and @be"), CE))
        full_names = ["be" + r.text for r in results]
        assert any("beta.py" in n for n in full_names)

    def test_dirs_have_trailing_slash(self, tmp_path):
        (tmp_path / "src").mkdir()
        comp = AtMentionCompleter(base_path=tmp_path)
        results = list(comp.get_completions(Document("@"), CE))
        displays = [str(r.display) for r in results]
        assert any("src/" in d for d in displays)

    def test_nonexistent_subdir_returns_empty(self, tmp_path):
        comp = AtMentionCompleter(base_path=tmp_path)
        results = list(comp.get_completions(Document("@nonexistent/"), CE))
        assert results == []


class TestInputBarSession:
    def test_creates_ok(self):
        s = InputBarSession()
        assert s._session is not None

    def test_builtin_commands_registered(self, tmp_path):
        s = InputBarSession(base_path=tmp_path)
        results = list(s._completer.get_completions(Document("/"), CE))
        assert len(results) >= len(BUILTIN_COMMANDS)

    def test_register_command(self, tmp_path):
        s = InputBarSession(base_path=tmp_path)
        s.register_command(CommandSpec("/mycommand", "A dynamic test command"))
        results = list(s._completer.get_completions(Document("/myc"), CE))
        full = ["/myc" + r.text for r in results]
        assert any("mycommand" in t for t in full)

    def test_at_completer_active(self, tmp_path):
        (tmp_path / "myfile.py").write_text("x")
        s = InputBarSession(base_path=tmp_path)
        results = list(s._completer.get_completions(Document("@myf"), CE))
        assert len(results) >= 1
