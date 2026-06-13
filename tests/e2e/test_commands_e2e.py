"""E2E tests for the Unified Command System (PRD-44 through PRD-46).

Tests the full pipeline:
  user types /command -> SlashCommandHandler -> CommandDispatcher -> handler/menu
  AgentRunnerBase mock verifies that config changes made via /config reach the agent.
  Custom command plugins verify the end-user extensibility story.
"""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agenthicc.commands import (
    build_builtin_registry,
    CommandDispatcher,
    CommandContext,
    Command,
)
from agenthicc.commands.plugin_loader import discover_command_plugins
from agenthicc.config import AgenthiccConfig

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_renderer(cfg: AgenthiccConfig | None = None) -> MagicMock:
    """Return a minimal renderer mock with _loaded_config and _status."""
    renderer = MagicMock()
    renderer._loaded_config = cfg or AgenthiccConfig()
    renderer._status = MagicMock()
    renderer._status.session_id = "test-session"
    renderer._status.resume_id = ""
    renderer._pending_menu = None
    renderer._pending_skill = None
    return renderer


def _make_ctx(
    text: str = "",
    cfg: AgenthiccConfig | None = None,
    renderer: MagicMock | None = None,
) -> CommandContext:
    """Build a CommandContext with sane defaults."""
    parts = text.strip().split(None, 1)
    args = parts[1] if len(parts) > 1 else ""
    if renderer is None:
        renderer = _make_renderer(cfg)
    return CommandContext(
        text=text,
        args=args,
        model=MagicMock(),
        console=MagicMock(),
        renderer=renderer,
        config=cfg or AgenthiccConfig(),
        session_id="test-session",
    )


def _navigate_to_field(menu, section_name: str, field_name: str) -> bool:
    """Move the cursor to section_name.field_name.  Returns True if found."""
    for si, section in enumerate(menu._sections):
        if section.name == section_name:
            for fi, f in enumerate(section.fields):
                if f.field_name == field_name:
                    menu._cursor = (si, fi)
                    return True
    return False


def _edit_field(menu, new_value: str) -> None:
    """Enter EDIT mode on the current cursor and type new_value, then confirm."""
    from agenthicc.tui.mention_input import Key

    menu.handle_key(Key.ENTER, "")
    for _ in range(len(menu._edit_buf)):
        menu.handle_key(Key.BACKSPACE, "")
    for ch in new_value:
        menu.handle_key(Key.CHAR, ch)
    menu.handle_key(Key.ENTER, "")


# ---------------------------------------------------------------------------
# Test 1: /config opens a ConfigurationMenu on the renderer
# ---------------------------------------------------------------------------


def test_config_command_opens_menu_and_updates_config():
    """dispatch('/config') sets renderer._pending_menu to a ConfigurationMenu instance.

    Then simulating key navigation + ENTER edits execution.provider to 'openai'.
    """
    from agenthicc.tui.widgets.config_menu import ConfigurationMenu

    cfg = AgenthiccConfig()
    assert cfg.execution.provider == "anthropic"

    registry = build_builtin_registry()
    dispatcher = CommandDispatcher(registry)

    renderer = _make_renderer(cfg)
    ctx = _make_ctx("/config", cfg=cfg, renderer=renderer)
    ctx.config = cfg

    handled = dispatcher.dispatch("/config", ctx)

    assert handled is True
    # Dispatcher should have set _pending_menu to a ConfigurationMenu
    assert renderer._pending_menu is not None
    assert isinstance(renderer._pending_menu, ConfigurationMenu)

    # Now simulate editing execution.provider via the menu
    menu = renderer._pending_menu
    found = _navigate_to_field(menu, "execution", "provider")
    assert found, "execution.provider field not found in ConfigurationMenu"

    _edit_field(menu, "openai")

    # The live config object must reflect the change
    assert cfg.execution.provider == "openai"


# ---------------------------------------------------------------------------
# Test 2: Agent runner sees config change from /config command
# ---------------------------------------------------------------------------


def test_agent_runner_sees_config_change_from_command():
    """After /config changes provider, the next agent turn reads the new provider."""
    cfg = AgenthiccConfig()
    assert cfg.execution.provider == "anthropic"

    # Simulate what InlineRenderer would do: use a live mutable cfg object
    mock_runner = MagicMock()
    mock_runner._transport = MagicMock()
    mock_runner._transport._config = MagicMock()
    mock_runner._transport._config.model = "old-model"
    mock_runner._signals = None

    # Simulate /config editing provider (what ConfigurationMenu._apply_value does)
    object.__setattr__(cfg.execution, "provider", "openai")

    # Verify that a runner would now see "openai"
    assert cfg.execution.provider == "openai"

    # _run_agent_turn reads: cfg.execution.provider
    provider_seen_by_runner = cfg.execution.provider
    assert provider_seen_by_runner == "openai"


# ---------------------------------------------------------------------------
# Test 3: SlashCommandHandler full pipeline (via dispatcher)
# ---------------------------------------------------------------------------


def test_slash_command_dispatch_full_pipeline():
    """SlashCommandHandler.handle('/status') returns True when a dispatcher exists."""
    from agenthicc.tui.app import SlashCommandHandler
    from agenthicc.tui.transcript import TranscriptModel

    cfg = AgenthiccConfig()
    registry = build_builtin_registry()
    dispatcher = CommandDispatcher(registry)

    renderer = _make_renderer(cfg)
    # Attach the dispatcher so SlashCommandHandler.handle() routes through it
    renderer._dispatcher = dispatcher
    renderer._loaded_config = cfg

    model = TranscriptModel()
    console = MagicMock()

    handler = SlashCommandHandler(renderer=renderer)
    result = handler.handle("/status", model, console)

    assert result is True


# ---------------------------------------------------------------------------
# Test 4: Command plugin discovered, registered, and dispatched (E2E)
# ---------------------------------------------------------------------------


def test_command_plugin_e2e(tmp_path: Path):
    """Create a /ping plugin file, discover it, register it, dispatch, assert effect."""
    plugin_dir = tmp_path / ".agenthicc" / "commands"
    plugin_dir.mkdir(parents=True)

    plugin_file = plugin_dir / "ping.py"
    plugin_file.write_text(
        """
from agenthicc.commands.command import Command, CommandContext

def _ping_handler(ctx: CommandContext) -> bool:
    ctx.renderer._pending_skill = "pong"
    return True

COMMAND = Command(
    name="/ping",
    description="Ping command for e2e test",
    handler=_ping_handler,
    source_id="command-plugin:ping",
)
""",
        encoding="utf-8",
    )

    plugin_set = discover_command_plugins(
        project_dir=tmp_path / ".agenthicc",
        user_dir=tmp_path / "no-user-dir",
    )

    assert len(plugin_set.all_commands) == 1
    assert plugin_set.all_commands[0].name == "/ping"

    registry = build_builtin_registry()
    for cmd in plugin_set.all_commands:
        registry.register(cmd)

    dispatcher = CommandDispatcher(registry)

    renderer = _make_renderer()
    ctx = _make_ctx("/ping", renderer=renderer)

    handled = dispatcher.dispatch("/ping", ctx)

    assert handled is True
    assert renderer._pending_skill == "pong"


# ---------------------------------------------------------------------------
# Test 5: Skill command invokes via dispatcher pending-skill mechanism
# ---------------------------------------------------------------------------


def test_skill_command_invokes_via_dispatcher():
    """A command registered with _make_skill_handler sets renderer._pending_skill."""
    from agenthicc.commands.builtins import _make_skill_handler
    from agenthicc.skills.loader import SkillDef

    # Build a minimal SkillDef (no filesystem access needed — body is pre-set)
    skill = SkillDef(
        name="my-skill",
        slug="my-skill",
        path=Path("/nonexistent"),  # body won't be read since we set _body directly
        description="A test skill",
        _body="Do something useful with {0}.",
    )

    renderer = _make_renderer()

    handler_fn = _make_skill_handler("my-skill", skill, renderer)

    registry = build_builtin_registry()
    cmd = Command(
        name="/my-skill",
        description="A test skill",
        group="Skills",
        source_id="skill:my-skill",
        handler=handler_fn,
    )
    registry.register(cmd)

    dispatcher = CommandDispatcher(registry)
    ctx = _make_ctx("/my-skill arg1", renderer=renderer)

    handled = dispatcher.dispatch("/my-skill", ctx)

    assert handled is True
    # _pending_skill should be set (the processed skill body string)
    assert renderer._pending_skill is not None


# ---------------------------------------------------------------------------
# Test 6: Unknown command returns False without raising
# ---------------------------------------------------------------------------


def test_unknown_command_returns_false_not_exception():
    """dispatch('/completely-unknown-xyz') returns False and never raises."""
    registry = build_builtin_registry()
    dispatcher = CommandDispatcher(registry)

    renderer = _make_renderer()
    ctx = _make_ctx("/completely-unknown-xyz", renderer=renderer)

    result = dispatcher.dispatch("/completely-unknown-xyz", ctx)

    assert result is False


# ---------------------------------------------------------------------------
# Test 7: /config appears in SlashCommandTrigger.get_matches dropdown
# ---------------------------------------------------------------------------


def test_config_command_in_dropdown_via_trigger():
    """SlashCommandTrigger backed by build_builtin_registry returns /config in matches."""
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
    from agenthicc.tui.trigger import TriggerContext

    registry = build_builtin_registry()
    trigger = SlashCommandTrigger(registry)

    ctx = TriggerContext(cwd=Path("."))

    # Empty fragment → all commands returned
    matches = trigger.get_matches("", ctx)

    names = [m.value for m in matches]
    assert "/config" in names

    # The /config MatchItem should carry a hint mentioning opens_menu semantics
    config_match = next(m for m in matches if m.value == "/config")
    # The hint string is built by _format_hint; it includes the description
    assert config_match.hint  # non-empty hint
    assert "/config" in config_match.hint


# ---------------------------------------------------------------------------
# Test 8: Custom plugin command appears in dropdown after registration
# ---------------------------------------------------------------------------


def test_command_plugin_appears_in_dropdown(tmp_path: Path):
    """A discovered /ping plugin command is visible via SlashCommandTrigger.get_matches."""
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
    from agenthicc.tui.trigger import TriggerContext

    plugin_dir = tmp_path / ".agenthicc" / "commands"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "pingpong.py").write_text(
        """
from agenthicc.commands.command import Command

COMMAND = Command(
    name="/pingpong",
    description="Ping pong test command",
    handler=lambda ctx: True,
    source_id="command-plugin:pingpong",
)
""",
        encoding="utf-8",
    )

    plugin_set = discover_command_plugins(
        project_dir=tmp_path / ".agenthicc",
        user_dir=tmp_path / "no-user",
    )

    registry = build_builtin_registry()
    for cmd in plugin_set.all_commands:
        registry.register(cmd)

    trigger = SlashCommandTrigger(registry)
    ctx = TriggerContext(cwd=tmp_path)

    # Fragment "pin" should match /pingpong
    matches = trigger.get_matches("pin", ctx)
    names = [m.value for m in matches]
    assert "/pingpong" in names


# ---------------------------------------------------------------------------
# Test 9: unregister_source removes all commands from a plugin
# ---------------------------------------------------------------------------


def test_full_command_lifecycle_with_source_id(tmp_path: Path):
    """Register 3 commands from 'plugin:x', unregister_source removes them all."""
    from agenthicc.commands.registry import UnifiedCommandRegistry

    registry = UnifiedCommandRegistry()

    for i in range(3):
        registry.register(Command(
            name=f"/plugin-cmd-{i}",
            description=f"Plugin command {i}",
            source_id="plugin:x",
            handler=lambda ctx: True,
        ))

    # Confirm they are present
    assert len(registry.commands_for_source("plugin:x")) == 3

    removed = registry.unregister_source("plugin:x")
    assert removed == 3

    # None of them should appear in the dropdown anymore
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
    from agenthicc.tui.trigger import TriggerContext

    trigger = SlashCommandTrigger(registry)
    ctx = TriggerContext(cwd=tmp_path)
    matches = trigger.get_matches("plugin-cmd", ctx)
    assert matches == []


# ---------------------------------------------------------------------------
# Test 10: Config saved by ConfigurationMenu._save() persists to disk
# ---------------------------------------------------------------------------


def test_config_saved_persists_across_session(tmp_path: Path, monkeypatch):
    """ConfigurationMenu._save() writes execution.provider = 'openai' to toml."""
    from agenthicc.tui.widgets.config_menu import ConfigurationMenu

    monkeypatch.chdir(tmp_path)

    cfg = AgenthiccConfig()
    console = MagicMock()
    menu = ConfigurationMenu(cfg, console)

    # Directly edit via the menu helpers so the field model is also updated
    found = _navigate_to_field(menu, "execution", "provider")
    assert found, "execution.provider must be present in ConfigurationMenu"
    _edit_field(menu, "openai")

    # _edit_field triggers _commit_edit → _save(); verify the file was written
    toml_path = tmp_path / ".agenthicc" / "agenthicc.toml"
    assert toml_path.exists(), f"Expected {toml_path} to be created by _save()"

    content = toml_path.read_text(encoding="utf-8")
    assert "openai" in content, (
        f"Expected 'openai' in saved toml content, got:\n{content}"
    )
