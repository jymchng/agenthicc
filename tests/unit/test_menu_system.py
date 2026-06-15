"""Unit tests for the Menu Widget System (PRD-41).

Tests cover:
  - MenuResult factory methods
  - MenuDriver lifecycle (open/close/active)
  - MenuDriver key routing and auto-close on DONE/CANCEL
  - CommandMenuRegistry register, get, commands, len
  - RendererContext field presence
  - MenuWidget protocol isinstance check
"""
from __future__ import annotations

import pytest

from agenthicc.tui.menu import (
    CommandMenuRegistry,
    MenuDriver,
    MenuResult,
    MenuResultKind,
    MenuWidget,
    RendererContext,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Minimal test widget
# ---------------------------------------------------------------------------


class EchoWidget:
    """Minimal MenuWidget for testing.

    Implements the MenuWidget protocol without inheriting from it so that the
    isinstance(EchoWidget(), MenuWidget) test validates the runtime-checkable
    Protocol machinery.
    """

    def render(self, prompt_str: str, buf: list, prev: int) -> int:
        return 1

    def handle_key(self, key: object, ch: str) -> MenuResult:
        # Use attribute access so the widget works without importing Key directly.
        if hasattr(key, "value") and key.value == "ENTER":
            return MenuResult.done("entered")
        if hasattr(key, "value") and key.value == "ESC":
            return MenuResult.cancel()
        return MenuResult.continue_()

    @property
    def edit_field_value(self) -> None:
        return None


# ---------------------------------------------------------------------------
# MenuResult factories
# ---------------------------------------------------------------------------


def test_menu_result_factories():
    assert MenuResult.continue_().kind == MenuResultKind.CONTINUE
    assert MenuResult.done(42).kind == MenuResultKind.DONE
    assert MenuResult.cancel().kind == MenuResultKind.CANCEL


def test_menu_result_done_stores_data():
    result = MenuResult.done(42)
    assert result.data == 42


def test_menu_result_continue_data_is_none():
    result = MenuResult.continue_()
    assert result.data is None


def test_menu_result_cancel_data_is_none():
    result = MenuResult.cancel()
    assert result.data is None


# ---------------------------------------------------------------------------
# MenuDriver — lifecycle
# ---------------------------------------------------------------------------


def test_menu_driver_initially_inactive():
    d = MenuDriver()
    assert not d.active


def test_menu_driver_open_activates():
    d = MenuDriver()
    d.open(EchoWidget())
    assert d.active


def test_menu_driver_close_deactivates():
    d = MenuDriver()
    d.open(EchoWidget())
    d.close()
    assert not d.active


def test_menu_driver_widget_property_after_open():
    d = MenuDriver()
    w = EchoWidget()
    d.open(w)
    assert d.widget is w


def test_menu_driver_widget_property_after_close():
    d = MenuDriver()
    d.open(EchoWidget())
    d.close()
    assert d.widget is None


# ---------------------------------------------------------------------------
# MenuDriver — key routing
# ---------------------------------------------------------------------------


def test_menu_driver_routes_to_widget():
    """MenuDriver delegates handle_key to the active widget."""
    from agenthicc.tui.cbreak_reader import Key

    d = MenuDriver()
    d.open(EchoWidget())
    result = d.handle_key(Key.ENTER, "")
    assert result.kind == MenuResultKind.DONE
    assert result.data == "entered"


def test_menu_driver_auto_closes_on_done():
    from agenthicc.tui.cbreak_reader import Key

    d = MenuDriver()
    d.open(EchoWidget())
    d.handle_key(Key.ENTER, "")
    assert not d.active


def test_menu_driver_auto_closes_on_cancel():
    from agenthicc.tui.cbreak_reader import Key

    d = MenuDriver()
    d.open(EchoWidget())
    result = d.handle_key(Key.ESC, "")
    assert result.kind == MenuResultKind.CANCEL
    assert not d.active


def test_menu_driver_stays_open_on_continue():
    from agenthicc.tui.cbreak_reader import Key

    d = MenuDriver()
    d.open(EchoWidget())
    # Any key other than ENTER or ESC returns CONTINUE.
    result = d.handle_key(Key.DOWN, "")
    assert result.kind == MenuResultKind.CONTINUE
    assert d.active


def test_menu_driver_no_widget_returns_continue():
    from agenthicc.tui.cbreak_reader import Key

    d = MenuDriver()
    result = d.handle_key(Key.ENTER, "")
    assert result.kind == MenuResultKind.CONTINUE


# ---------------------------------------------------------------------------
# CommandMenuRegistry
# ---------------------------------------------------------------------------


def test_command_registry_register_and_get():
    reg = CommandMenuRegistry()
    factory = lambda ctx: EchoWidget()  # noqa: E731
    reg.register("/config", factory)
    assert reg.get("/config") is factory


def test_command_registry_get_missing_returns_none():
    reg = CommandMenuRegistry()
    assert reg.get("/nonexistent") is None


def test_command_registry_commands_list():
    reg = CommandMenuRegistry()
    reg.register("/config", lambda ctx: EchoWidget())
    reg.register("/agents", lambda ctx: EchoWidget())
    cmds = reg.commands()
    assert "/config" in cmds
    assert "/agents" in cmds
    assert len(cmds) == 2


def test_command_registry_len():
    reg = CommandMenuRegistry()
    assert len(reg) == 0
    reg.register("/config", lambda ctx: EchoWidget())
    assert len(reg) == 1
    reg.register("/agents", lambda ctx: EchoWidget())
    assert len(reg) == 2


def test_command_registry_overwrite():
    reg = CommandMenuRegistry()
    f1 = lambda ctx: EchoWidget()  # noqa: E731
    f2 = lambda ctx: EchoWidget()  # noqa: E731
    reg.register("/config", f1)
    reg.register("/config", f2)
    assert reg.get("/config") is f2
    assert len(reg) == 1


# ---------------------------------------------------------------------------
# RendererContext
# ---------------------------------------------------------------------------


def test_renderer_context_fields():
    ctx = RendererContext(config=None, console=None, session_id="ses-1")
    assert ctx.config is None
    assert ctx.console is None
    assert ctx.session_id == "ses-1"


def test_renderer_context_default_session_id():
    ctx = RendererContext(config=None, console=None)
    assert ctx.session_id == ""


# ---------------------------------------------------------------------------
# MenuWidget protocol — isinstance check
# ---------------------------------------------------------------------------


def test_menu_widget_protocol_satisfied():
    """EchoWidget satisfies the runtime-checkable MenuWidget protocol."""
    assert isinstance(EchoWidget(), MenuWidget)
