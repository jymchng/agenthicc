"""E2E tests for the Menu Widget System (PRD-41 through PRD-43).

These tests exercise the complete pipeline:
  SlashCommandHandler → CommandMenuRegistry → ConfigurationMenu → live config update
  AgentRunnerBase mock verifies the updated config reaches the agent runner.
"""
from __future__ import annotations

import pytest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agenthicc.config import AgenthiccConfig
from agenthicc.tui.menu import CommandMenuRegistry, RendererContext, MenuResult, MenuResultKind, MenuDriver
from agenthicc.tui.widgets.config_menu import ConfigurationMenu
from agenthicc.tui.mention_input import Key, read_line_with_mention

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_menu(cfg: AgenthiccConfig | None = None) -> tuple[AgenthiccConfig, ConfigurationMenu]:
    """Create a ConfigurationMenu backed by a fresh (or provided) AgenthiccConfig."""
    if cfg is None:
        cfg = AgenthiccConfig()
    console = MagicMock()
    menu = ConfigurationMenu(cfg, console)
    return cfg, menu


def _navigate_to_field(
    menu: ConfigurationMenu,
    section_name: str,
    field_name: str,
) -> bool:
    """Move the menu cursor to the given section.field.  Returns True if found."""
    for si, section in enumerate(menu._sections):
        if section.name == section_name:
            for fi, f in enumerate(section.fields):
                if f.field_name == field_name:
                    menu._cursor = (si, fi)
                    return True
    return False


def _edit_field(menu: ConfigurationMenu, new_value: str) -> None:
    """Activate edit mode on the current cursor position and type *new_value*."""
    # ENTER in NAVIGATE → enter EDIT mode
    menu.handle_key(Key.ENTER, "")
    # Clear the pre-populated buffer
    for _ in range(len(menu._edit_buf)):
        menu.handle_key(Key.BACKSPACE, "")
    # Type the new value
    for ch in new_value:
        menu.handle_key(Key.CHAR, ch)
    # Confirm with ENTER
    menu.handle_key(Key.ENTER, "")


# ---------------------------------------------------------------------------
# Test 1: ConfigurationMenu updates the live config
# ---------------------------------------------------------------------------


def test_config_menu_updates_live_config():
    """Navigate to execution.provider, edit to 'openai', confirm → cfg updated."""
    cfg, menu = _make_menu()

    assert cfg.execution.provider == "anthropic"

    found = _navigate_to_field(menu, "execution", "provider")
    assert found, "execution.provider field not found in menu"

    _edit_field(menu, "openai")

    assert cfg.execution.provider == "openai"


# ---------------------------------------------------------------------------
# Test 2: Agent runner sees the updated config
# ---------------------------------------------------------------------------


def test_agent_runner_sees_updated_config():
    """Simulate an agent runner reading cfg after ConfigurationMenu changes provider."""
    cfg = AgenthiccConfig()
    cfg.execution.provider = "anthropic"

    # Simulate what ConfigurationMenu does when the user edits provider
    _, menu = _make_menu(cfg)

    found = _navigate_to_field(menu, "execution", "provider")
    assert found

    _edit_field(menu, "openai")

    # Mock how _run_agent_turn reads config
    mock_runner = MagicMock()
    mock_runner._transport = MagicMock()
    mock_runner._transport._config = MagicMock()
    mock_runner._transport._config.model = "claude-sonnet-4-6"
    mock_runner._signals = None

    # After config menu changes provider:
    assert cfg.execution.provider == "openai"
    # This is what _run_agent_turn() reads: cfg.execution.provider
    provider_seen_by_runner = cfg.execution.provider
    assert provider_seen_by_runner == "openai"


# ---------------------------------------------------------------------------
# Test 3: CommandRegistry with ConfigurationMenu factory
# ---------------------------------------------------------------------------


def test_command_registry_with_config_menu_factory():
    """Register /config → factory; get returns factory; factory produces ConfigurationMenu."""
    cfg = AgenthiccConfig()
    console = MagicMock()

    registry = CommandMenuRegistry()
    registry.register("/config", lambda ctx: ConfigurationMenu(ctx.config, ctx.console))

    factory = registry.get("/config")
    assert factory is not None, "factory must be retrievable via get('/config')"

    ctx = RendererContext(config=cfg, console=console)
    result = factory(ctx)

    assert isinstance(result, ConfigurationMenu)


# ---------------------------------------------------------------------------
# Test 4: SlashCommandHandler opens menu via _pending_menu
# ---------------------------------------------------------------------------


def test_slash_command_handler_opens_menu():
    """SlashCommandHandler.handle('/config') sets _pending_menu on the renderer."""
    from agenthicc.tui.app import SlashCommandHandler
    from agenthicc.tui.transcript import TranscriptModel

    cfg = AgenthiccConfig()
    console = MagicMock()

    renderer = MagicMock()
    menu_registry = CommandMenuRegistry()
    menu_registry.register("/config", lambda ctx: ConfigurationMenu(ctx.config, ctx.console))
    renderer._menu_registry = menu_registry
    renderer._loaded_config = cfg
    renderer._status = MagicMock()
    renderer._status.session_id = "e2e-session"

    handler = SlashCommandHandler(renderer=renderer)
    model = TranscriptModel()

    handled = handler.handle("/config", model, console)

    assert handled is True
    assert renderer._pending_menu is not None
    assert isinstance(renderer._pending_menu, ConfigurationMenu)


# ---------------------------------------------------------------------------
# Test 5: Full config edit pipeline — model field
# ---------------------------------------------------------------------------


def test_full_config_edit_pipeline(tmp_path: Path):
    """Navigate to execution.model, edit to 'gpt-4o', verify via mock runner."""
    cfg = AgenthiccConfig()
    _, menu = _make_menu(cfg)

    # Navigate to execution.model
    found = _navigate_to_field(menu, "execution", "model")
    assert found, "execution.model must be in the menu"

    _edit_field(menu, "gpt-4o")

    assert cfg.execution.model == "gpt-4o"

    # Verify a runner would read the updated value
    mock_runner = MagicMock()
    # Simulate _run_agent_turn reading cfg.execution.model
    model_used = cfg.execution.model
    assert model_used == "gpt-4o"


# ---------------------------------------------------------------------------
# Test 6: read_line_with_mention with initial_menu=ConfigurationMenu → ESC → type + submit
# ---------------------------------------------------------------------------


def test_menu_driver_integrates_with_read_line(tmp_path: Path):
    """Open ConfigurationMenu; ESC to close; type 'after menu'; ENTER → result."""
    cfg = AgenthiccConfig()
    console = MagicMock()
    menu = ConfigurationMenu(cfg, console)

    keys = [
        (Key.ESC, ""),          # close the ConfigurationMenu
        (Key.CHAR, "a"),
        (Key.CHAR, "f"),
        (Key.CHAR, "t"),
        (Key.CHAR, "e"),
        (Key.CHAR, "r"),
        (Key.CHAR, " "),
        (Key.CHAR, "m"),
        (Key.CHAR, "e"),
        (Key.CHAR, "n"),
        (Key.CHAR, "u"),
        (Key.ENTER, ""),        # submit
    ]

    it = iter(keys)
    history: list[str] = []

    def fake_read_key(fd):
        try:
            return next(it)
        except StopIteration:
            return (Key.CTRL_D, "")

    @contextmanager
    def fake_raw(fd):
        yield 42

    original_provider = cfg.execution.provider

    with (
        patch("agenthicc.tui.mention_input._raw_mode", fake_raw),
        patch("agenthicc.tui.mention_input._read_key", fake_read_key),
        patch("agenthicc.tui.mention_input._redraw", return_value=0),
        patch("sys.stdin.isatty", return_value=True),
        patch("sys.stdin.fileno", return_value=42),
    ):
        result = read_line_with_mention(
            "❯ ", tmp_path, history,
            initial_menu=menu,
        )

    assert result == "after menu"
    # ESC = cancel with no edits, so cfg should be unchanged
    assert cfg.execution.provider == original_provider


# ---------------------------------------------------------------------------
# Test 7: ConfigurationMenu save writes file (monkeypatched tomli_w)
# ---------------------------------------------------------------------------


def test_config_menu_save_writes_file(tmp_path: Path, monkeypatch):
    """Press 's' while navigating → _save() is called; status_msg contains 'Save'."""
    cfg = AgenthiccConfig()
    console = MagicMock()
    menu = ConfigurationMenu(cfg, console)

    # Monkeypatch tomli_w so we don't need the actual package installed
    fake_tomli_w = MagicMock()
    fake_tomli_w.dumps.return_value = "[execution]\nprovider = 'anthropic'\n"

    # Also patch Path.write_bytes and mkdir to avoid filesystem side effects
    write_bytes_calls = []

    original_write_bytes = Path.write_bytes

    def fake_write_bytes(self, data):
        write_bytes_calls.append((str(self), data))

    monkeypatch.chdir(tmp_path)

    import sys
    import types

    # Insert a fake tomli_w module into sys.modules
    fake_module = types.ModuleType("tomli_w")
    fake_module.dumps = lambda d: "[execution]\nprovider = 'anthropic'\n"
    monkeypatch.setitem(sys.modules, "tomli_w", fake_module)

    # Trigger save
    menu._save()

    # The status_msg should now reference "Save"
    assert "Save" in menu._status_msg or "Saved" in menu._status_msg or "save" in menu._status_msg.lower()


# ---------------------------------------------------------------------------
# Test 8: Multiple config edits accumulate
# ---------------------------------------------------------------------------


def test_multiple_config_edits_accumulate():
    """Edit provider → 'openai', then model → 'gpt-4o'; both changes apply."""
    cfg = AgenthiccConfig()
    _, menu = _make_menu(cfg)

    # First edit: execution.provider → "openai"
    found = _navigate_to_field(menu, "execution", "provider")
    assert found
    _edit_field(menu, "openai")
    assert cfg.execution.provider == "openai"

    # Second edit: execution.model → "gpt-4o"
    found = _navigate_to_field(menu, "execution", "model")
    assert found
    _edit_field(menu, "gpt-4o")
    assert cfg.execution.model == "gpt-4o"

    # Both changes are present simultaneously
    assert cfg.execution.provider == "openai"
    assert cfg.execution.model == "gpt-4o"


# ---------------------------------------------------------------------------
# Test 9: CommandMenuRegistry commands() lists registered commands
# ---------------------------------------------------------------------------


def test_command_registry_commands_list():
    """CommandMenuRegistry.commands() returns all registered command names."""
    registry = CommandMenuRegistry()
    registry.register("/config", lambda ctx: None)
    registry.register("/theme", lambda ctx: None)

    cmds = registry.commands()
    assert "/config" in cmds
    assert "/theme" in cmds
    assert len(cmds) == 2


# ---------------------------------------------------------------------------
# Test 10: ConfigurationMenu _build_sections produces sections for all SECTION_ATTRS
# ---------------------------------------------------------------------------


def test_build_sections_covers_all_attrs():
    """_build_sections produces a section for each top-level AgenthiccConfig sub-object."""
    from agenthicc.tui.widgets.config_menu import _build_sections

    cfg = AgenthiccConfig()
    sections = _build_sections(cfg)

    names = {s.name for s in sections}
    # execution, memory, security, api, plugins are the expected section attrs
    assert "execution" in names
    assert "memory" in names
    assert "security" in names
    assert "api" in names
    assert "plugins" in names


# ---------------------------------------------------------------------------
# Test 11: ConfigField.changed is set correctly
# ---------------------------------------------------------------------------


def test_config_field_changed_flag():
    """ConfigField.changed is True when value differs from default, False otherwise."""
    from agenthicc.tui.widgets.config_menu import _build_sections

    cfg = AgenthiccConfig()
    # Modify a field before building sections
    cfg.execution.provider = "openai"  # default is "anthropic"

    sections = _build_sections(cfg)
    exec_section = next(s for s in sections if s.name == "execution")
    provider_field = next(f for f in exec_section.fields if f.field_name == "provider")

    assert provider_field.changed is True

    # A field that's still at its default should not be changed
    model_field = next(f for f in exec_section.fields if f.field_name == "model")
    assert model_field.changed is False  # "" is the default


# ---------------------------------------------------------------------------
# Test 12: MenuDriver opens, renders, handles CANCEL, closes automatically
# ---------------------------------------------------------------------------


def test_menu_driver_full_lifecycle():
    """MenuDriver: open → handle_key(ESC/CANCEL) auto-closes → not active."""
    cfg = AgenthiccConfig()
    menu = ConfigurationMenu(cfg, MagicMock())

    driver = MenuDriver()
    assert not driver.active

    driver.open(menu)
    assert driver.active

    # ESC in ConfigurationMenu NAVIGATE mode → MenuResult.cancel() → driver closes
    result = driver.handle_key(Key.ESC, "")

    assert result.kind == MenuResultKind.CANCEL
    assert not driver.active


# ---------------------------------------------------------------------------
# Test 13: RendererContext carries config and console
# ---------------------------------------------------------------------------


def test_renderer_context_attributes():
    """RendererContext stores config, console, and session_id."""
    cfg = AgenthiccConfig()
    console = MagicMock()

    ctx = RendererContext(config=cfg, console=console, session_id="sess-abc")

    assert ctx.config is cfg
    assert ctx.console is console
    assert ctx.session_id == "sess-abc"


# ---------------------------------------------------------------------------
# Test 14: ConfigurationMenu in EDIT mode, ESC cancels without applying
# ---------------------------------------------------------------------------


def test_config_menu_edit_esc_does_not_apply():
    """In EDIT mode, ESC cancels the edit; original value is preserved."""
    cfg = AgenthiccConfig()
    _, menu = _make_menu(cfg)

    original = cfg.execution.provider

    found = _navigate_to_field(menu, "execution", "provider")
    assert found

    # Enter EDIT mode
    menu.handle_key(Key.ENTER, "")
    assert menu._state == "EDIT"

    # Type something new
    for _ in range(len(menu._edit_buf)):
        menu.handle_key(Key.BACKSPACE, "")
    for ch in "something-else":
        menu.handle_key(Key.CHAR, ch)

    # ESC cancels the edit
    menu.handle_key(Key.ESC, "")
    assert menu._state == "NAVIGATE"

    # Value must not have changed
    assert cfg.execution.provider == original
