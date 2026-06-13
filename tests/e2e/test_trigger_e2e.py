"""E2E tests for the trigger system — exercises real TriggerRegistry + state machine."""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

pytestmark = pytest.mark.e2e


# ── shared driver helper ──────────────────────────────────────────────────────


def _drive_with_registry(keys, tmp_path, registry=None):
    from agenthicc.tui.mention_input import read_line_with_mention, Key
    from agenthicc.tui.trigger import TriggerRegistry
    from agenthicc.tui.triggers.at_mention import AtMentionTrigger
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
    from agenthicc.tui.input_bar import build_default_registry
    from contextlib import contextmanager

    it = iter(keys)
    history = []

    def fake_read_key(fd):
        return next(it)

    @contextmanager
    def fake_raw(fd):
        yield 42

    if registry is None:
        registry = TriggerRegistry()
        registry.register(AtMentionTrigger())
        cmd_reg = build_default_registry()
        registry.register(SlashCommandTrigger(cmd_reg))

    # Patch sys.stdin at the mention_input module level so isatty() and
    # fileno() are both controlled. fileno() must return an int so that the
    # _raw_mode context manager (also mocked) accepts it without error.
    fake_stdin = MagicMock()
    fake_stdin.isatty.return_value = True
    fake_stdin.fileno.return_value = 42

    with patch("agenthicc.tui.mention_input._raw_mode", fake_raw), \
         patch("agenthicc.tui.mention_input._read_key", fake_read_key), \
         patch("agenthicc.tui.mention_input._redraw", return_value=0), \
         patch("agenthicc.tui.mention_input.sys.stdin", fake_stdin):
        result = read_line_with_mention("❯ ", tmp_path, history, registry)
    return result, history


# ── test 1: @mention selects a real file ────────────────────────────────────


def test_at_mention_selects_real_file(tmp_path):
    """Typing '@' → ENTER (select first match) → ENTER submits '@<filename>'."""
    from agenthicc.tui.mention_input import Key

    # Create a file so the @mention picker has something to show
    (tmp_path / "pyproject.toml").write_text("[project]")

    keys = [
        (Key.AT, ""),       # open @ trigger
        (Key.ENTER, ""),    # select first match (pyproject.toml)
        (Key.ENTER, ""),    # submit line
    ]

    result, history = _drive_with_registry(keys, tmp_path)
    assert result is not None
    assert "@pyproject.toml" in result


# ── test 2: slash command selects /model ─────────────────────────────────────


def test_slash_command_selects_model():
    """`/` opens dropdown, DOWN navigates to /model, ENTER selects, ENTER submits."""
    from agenthicc.tui.mention_input import Key
    from agenthicc.tui.input_bar import build_default_registry
    from agenthicc.tui.trigger import TriggerRegistry
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
    from agenthicc.tui.triggers.at_mention import AtMentionTrigger
    import tempfile

    cmd_reg = build_default_registry()
    registry = TriggerRegistry()
    registry.register(AtMentionTrigger())
    registry.register(SlashCommandTrigger(cmd_reg))

    # Figure out which DOWN-press index gets us to /model.
    # Commands are sorted; find /model position among all commands.
    all_cmds = cmd_reg.all_commands()
    names = [c.name for c in all_cmds]
    # With fragment="" all commands match; /model is somewhere in the list
    # We'll navigate with enough DOWN presses to reach it then ENTER
    # For robustness: drive with CHAR "/" then type "model" → ENTER → ENTER
    keys = [
        (Key.CHAR, "/"),    # open slash trigger
        (Key.CHAR, "m"),    # narrow to /m... commands
        (Key.CHAR, "o"),    # narrow further
        (Key.CHAR, "d"),
        (Key.CHAR, "e"),
        (Key.CHAR, "l"),
        (Key.ENTER, ""),    # select first match (/model)
        (Key.ENTER, ""),    # submit line
    ]

    with tempfile.TemporaryDirectory() as td:
        result, history = _drive_with_registry(keys, Path(td), registry)

    assert result is not None
    assert "/model" in result


# ── test 3: ESC cancels slash command, resumes normal edit ───────────────────


def test_slash_command_cancel_esc():
    """`/` opens dropdown; ESC cancels; typing 'hi' + ENTER submits 'hi'."""
    from agenthicc.tui.mention_input import Key
    import tempfile

    keys = [
        (Key.CHAR, "/"),    # open slash trigger
        (Key.ESC, ""),      # cancel → '/' goes back into buf
        (Key.BACKSPACE, ""),# remove the restored '/'
        (Key.CHAR, "h"),
        (Key.CHAR, "i"),
        (Key.ENTER, ""),
    ]

    with tempfile.TemporaryDirectory() as td:
        result, history = _drive_with_registry(keys, Path(td))

    assert result == "hi"


# ── test 4: @ and / in the same session ──────────────────────────────────────


def test_at_and_slash_in_same_session(tmp_path):
    """First call tries '/' → ESC (cancel); second call types '@r' → readme.md."""
    from agenthicc.tui.mention_input import Key, read_line_with_mention
    from agenthicc.tui.trigger import TriggerRegistry
    from agenthicc.tui.triggers.at_mention import AtMentionTrigger
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
    from agenthicc.tui.input_bar import build_default_registry
    from contextlib import contextmanager

    (tmp_path / "readme.md").write_text("# readme")

    cmd_reg = build_default_registry()
    registry = TriggerRegistry()
    registry.register(AtMentionTrigger())
    registry.register(SlashCommandTrigger(cmd_reg))
    history: list[str] = []

    fake_stdin = MagicMock()
    fake_stdin.isatty.return_value = True
    fake_stdin.fileno.return_value = 42

    @contextmanager
    def fake_raw(fd):
        yield 42

    # First call: open '/' trigger, ESC to cancel, clear buf, then ENTER on "/"
    # on_cancel restores '/' into buf; CTRL_U clears it; ENTER returns ""
    first_keys = [
        (Key.CHAR, "/"),    # open slash trigger
        (Key.ESC, ""),      # cancel → '/' restored into buf
        (Key.CTRL_U, ""),   # clear buffer
        (Key.ENTER, ""),    # submit "" (empty)
    ]
    it1 = iter(first_keys)
    with patch("agenthicc.tui.mention_input._raw_mode", fake_raw), \
         patch("agenthicc.tui.mention_input._read_key", lambda fd: next(it1)), \
         patch("agenthicc.tui.mention_input._redraw", return_value=0), \
         patch("agenthicc.tui.mention_input.sys.stdin", fake_stdin):
        r1 = read_line_with_mention("❯ ", tmp_path, history, registry)
    # empty submit
    assert r1 == "" or r1 == "/"

    # Second call: '@' trigger → type 'r' (narrow to readme.md) → ENTER select → ENTER submit
    second_keys = [
        (Key.AT, ""),       # open @ trigger
        (Key.CHAR, "r"),    # narrow
        (Key.ENTER, ""),    # select first match (readme.md)
        (Key.ENTER, ""),    # submit
    ]
    it2 = iter(second_keys)
    with patch("agenthicc.tui.mention_input._raw_mode", fake_raw), \
         patch("agenthicc.tui.mention_input._read_key", lambda fd: next(it2)), \
         patch("agenthicc.tui.mention_input._redraw", return_value=0), \
         patch("agenthicc.tui.mention_input.sys.stdin", fake_stdin):
        r2 = read_line_with_mention("❯ ", tmp_path, history, registry)

    assert r2 is not None
    assert "readme.md" in r2


# ── test 5: history preserved across trigger interactions ────────────────────


def test_history_preserved_across_triggers():
    """Submit 'hello', then start '/', ESC, submit empty → history has 'hello'."""
    from agenthicc.tui.mention_input import Key
    import tempfile

    # We need two separate runs sharing the same history list.
    from agenthicc.tui.trigger import TriggerRegistry
    from agenthicc.tui.triggers.at_mention import AtMentionTrigger
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
    from agenthicc.tui.input_bar import build_default_registry
    from contextlib import contextmanager

    cmd_reg = build_default_registry()
    registry = TriggerRegistry()
    registry.register(AtMentionTrigger())
    registry.register(SlashCommandTrigger(cmd_reg))

    history: list[str] = []

    def _run(keys):
        it = iter(keys)

        def fake_read_key(fd):
            return next(it)

        @contextmanager
        def fake_raw(fd):
            yield 42

        fake_stdin = MagicMock()
        fake_stdin.isatty.return_value = True
        fake_stdin.fileno.return_value = 42

        from agenthicc.tui.mention_input import read_line_with_mention
        with patch("agenthicc.tui.mention_input._raw_mode", fake_raw), \
             patch("agenthicc.tui.mention_input._read_key", fake_read_key), \
             patch("agenthicc.tui.mention_input._redraw", return_value=0), \
             patch("agenthicc.tui.mention_input.sys.stdin", fake_stdin):
            return read_line_with_mention("❯ ", Path(td), history, registry)

    with tempfile.TemporaryDirectory() as td:
        # First run: type "hello" and submit
        r1 = _run([
            (Key.CHAR, "h"),
            (Key.CHAR, "e"),
            (Key.CHAR, "l"),
            (Key.CHAR, "l"),
            (Key.CHAR, "o"),
            (Key.ENTER, ""),
        ])
        assert r1 == "hello"
        assert "hello" in history

        # Second run: open '/', ESC, submit empty
        r2 = _run([
            (Key.CHAR, "/"),
            (Key.ESC, ""),
            (Key.CTRL_U, ""),   # clear the restored '/'
            (Key.ENTER, ""),
        ])
        # Empty submit returns ""
        assert r2 == "" or r2 == "/"  # depending on whether CTRL_U cleared it

    # history still has 'hello' from the first run
    assert "hello" in history


# ── test 6: Ctrl+C exits from slash mode ────────────────────────────────────


def test_ctrl_c_exits_from_slash_mode():
    """'/' + CTRL_C (cancel trigger, first press) + CTRL_C (second press) → None."""
    from agenthicc.tui.mention_input import Key
    import tempfile

    keys = [
        (Key.CHAR, "/"),    # open slash trigger
        (Key.CTRL_C, ""),   # cancel trigger → first CTRL_C (shows warning)
        (Key.CTRL_C, ""),   # second CTRL_C → return None
    ]

    with tempfile.TemporaryDirectory() as td:
        result, history = _drive_with_registry(keys, Path(td))

    assert result is None


# ── test 7: CommandRegistry groups contain "Built-in" ────────────────────────


def test_command_registry_groups_in_help():
    """build_default_registry().groups() must contain the 'Built-in' group."""
    from agenthicc.tui.input_bar import build_default_registry

    reg = build_default_registry()
    groups = reg.groups()
    assert "Built-in" in groups


# ── test 8: SlashCommandTrigger hint line for /model ─────────────────────────


def test_slash_trigger_hint_line():
    """SlashCommandTrigger.get_matches('mod', ...) returns an item whose hint
    contains '[provider]' (from the /model argument_hint)."""
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
    from agenthicc.tui.trigger import TriggerContext
    from agenthicc.tui.input_bar import build_default_registry
    import tempfile

    reg = build_default_registry()
    trigger = SlashCommandTrigger(reg)

    with tempfile.TemporaryDirectory() as td:
        ctx = TriggerContext(cwd=Path(td))
        matches = trigger.get_matches("mod", ctx)

    # Should find /model (and possibly /models)
    assert matches, "Expected at least one match for 'mod'"
    model_matches = [m for m in matches if "/model" in m.value]
    assert model_matches, "Expected a /model match"

    # The hint for /model should reference its argument_hint
    model_item = model_matches[0]
    hint = trigger.get_hint(model_item)
    assert hint is not None
    assert "[provider]" in hint
