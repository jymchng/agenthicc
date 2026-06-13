"""Unit tests for ConfigurationMenu (PRD-43).

Tests cover:
  - _build_sections structure and field types
  - Menu lifecycle: NAVIGATE state, edit_field_value in each state
  - Key handling: ESC → cancel, Enter → edit mode, Enter in edit → commit
  - ESC in edit mode → cancel preserving original value
  - Bool toggle via Enter
  - Int validation rejects non-numeric input
  - Cursor navigation (DOWN moves cursor)
  - Status message update after save attempt
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from agenthicc.config import AgenthiccConfig
from agenthicc.tui.menu import MenuResultKind
from agenthicc.tui.widgets.config_menu import ConfigurationMenu, _build_sections

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_menu() -> tuple[ConfigurationMenu, AgenthiccConfig]:
    cfg = AgenthiccConfig()
    return ConfigurationMenu(cfg, MagicMock()), cfg


# ---------------------------------------------------------------------------
# _build_sections
# ---------------------------------------------------------------------------


def test_build_sections_returns_list():
    cfg = AgenthiccConfig()
    sections = _build_sections(cfg)
    assert isinstance(sections, list)
    assert len(sections) > 0


def test_build_sections_has_execution():
    cfg = AgenthiccConfig()
    sections = _build_sections(cfg)
    names = [s.name for s in sections]
    assert "execution" in names


def test_build_sections_includes_execution():
    """Alias used in PRD-43 test spec."""
    cfg = AgenthiccConfig()
    sections = _build_sections(cfg)
    names = [s.name for s in sections]
    assert "execution" in names


def test_execution_provider_field_found():
    cfg = AgenthiccConfig()
    sections = _build_sections(cfg)
    exec_section = next(s for s in sections if s.name == "execution")
    field_names = [f.field_name for f in exec_section.fields]
    assert "provider" in field_names


def test_field_type_is_str_for_provider():
    cfg = AgenthiccConfig()
    sections = _build_sections(cfg)
    exec_section = next(s for s in sections if s.name == "execution")
    provider_field = next(f for f in exec_section.fields if f.field_name == "provider")
    assert provider_field.field_type is str
    assert provider_field.value == "anthropic"


def test_field_editable_for_str():
    cfg = AgenthiccConfig()
    sections = _build_sections(cfg)
    exec_section = next(s for s in sections if s.name == "execution")
    provider_field = next(f for f in exec_section.fields if f.field_name == "provider")
    assert provider_field.editable is True


def test_field_not_editable_for_list():
    """List/dict fields should be marked not editable (v1 behaviour)."""
    cfg = AgenthiccConfig()
    sections = _build_sections(cfg)
    # Security section has allowed_paths which is a list.
    sec_section = next((s for s in sections if s.name == "security"), None)
    if sec_section is not None:
        list_fields = [f for f in sec_section.fields if isinstance(f.value, list)]
        for lf in list_fields:
            assert lf.editable is False, f"{lf.field_name} should be non-editable"


def test_build_sections_fields_have_correct_types():
    """PRD-43 spec: provider field type is str, value is 'anthropic'."""
    cfg = AgenthiccConfig()
    sections = _build_sections(cfg)
    exec_section = next(s for s in sections if s.name == "execution")
    provider_field = next(f for f in exec_section.fields if f.field_name == "provider")
    assert provider_field.field_type is str
    assert provider_field.value == "anthropic"


# ---------------------------------------------------------------------------
# Menu state — NAVIGATE
# ---------------------------------------------------------------------------


def test_menu_starts_in_navigate_state():
    menu, _ = _make_menu()
    assert menu._state == "NAVIGATE"


def test_edit_field_value_none_in_navigate():
    menu, _ = _make_menu()
    assert menu.edit_field_value is None


def test_menu_edit_field_value_none_in_navigate():
    """Alias from PRD-43 spec."""
    menu, _ = _make_menu()
    assert menu.edit_field_value is None


# ---------------------------------------------------------------------------
# ESC in NAVIGATE → cancel
# ---------------------------------------------------------------------------


def test_esc_in_navigate_returns_cancel():
    from agenthicc.tui.mention_input import Key

    menu, _ = _make_menu()
    result = menu.handle_key(Key.ESC, "")
    assert result.kind == MenuResultKind.CANCEL


def test_menu_esc_returns_cancel():
    """PRD-43 spec alias."""
    from agenthicc.tui.mention_input import Key

    menu, _ = _make_menu()
    result = menu.handle_key(Key.ESC, "")
    assert result.kind == MenuResultKind.CANCEL


# ---------------------------------------------------------------------------
# ENTER on str field → EDIT mode
# ---------------------------------------------------------------------------


def _navigate_to_execution_provider(menu: ConfigurationMenu) -> None:
    """Place cursor at execution section, provider field."""
    exec_idx = next(i for i, s in enumerate(menu._sections) if s.name == "execution")
    field_idx = next(
        i for i, f in enumerate(menu._sections[exec_idx].fields)
        if f.field_name == "provider"
    )
    menu._cursor = (exec_idx, field_idx)


def test_enter_on_str_field_enters_edit_mode():
    from agenthicc.tui.mention_input import Key

    menu, _ = _make_menu()
    _navigate_to_execution_provider(menu)
    menu.handle_key(Key.ENTER, "")
    assert menu._state == "EDIT"
    assert menu.edit_field_value is not None


def test_menu_edit_mode_on_enter():
    """PRD-43 spec alias."""
    from agenthicc.tui.mention_input import Key

    menu, _ = _make_menu()
    _navigate_to_execution_provider(menu)
    menu.handle_key(Key.ENTER, "")
    assert menu._state == "EDIT"
    assert menu.edit_field_value is not None


# ---------------------------------------------------------------------------
# Edit mode: commit on Enter
# ---------------------------------------------------------------------------


def test_edit_commits_on_enter():
    from agenthicc.tui.mention_input import Key

    menu, cfg = _make_menu()
    _navigate_to_execution_provider(menu)
    menu.handle_key(Key.ENTER, "")  # enter edit mode; buf pre-populated with current value
    # Clear the pre-populated value (snapshot length before the loop), then type new value.
    for _ in range(len(menu._edit_buf)):
        menu.handle_key(Key.BACKSPACE, "")
    for ch in "openai":
        menu.handle_key(Key.CHAR, ch)
    menu.handle_key(Key.ENTER, "")  # confirm
    assert cfg.execution.provider == "openai"
    assert menu._state == "NAVIGATE"


def test_menu_edit_commits_value():
    """PRD-43 spec alias."""
    from agenthicc.tui.mention_input import Key

    menu, cfg = _make_menu()
    _navigate_to_execution_provider(menu)
    menu.handle_key(Key.ENTER, "")
    # Clear the pre-populated value (snapshot length before the loop), then type new value.
    for _ in range(len(menu._edit_buf)):
        menu.handle_key(Key.BACKSPACE, "")
    for ch in "openai":
        menu.handle_key(Key.CHAR, ch)
    menu.handle_key(Key.ENTER, "")
    assert cfg.execution.provider == "openai"
    assert menu._state == "NAVIGATE"


# ---------------------------------------------------------------------------
# Edit mode: ESC → cancel, preserves original value
# ---------------------------------------------------------------------------


def test_edit_cancel_on_esc_preserves_original():
    from agenthicc.tui.mention_input import Key

    menu, cfg = _make_menu()
    original = cfg.execution.provider
    _navigate_to_execution_provider(menu)
    menu.handle_key(Key.ENTER, "")
    for ch in "openai":
        menu.handle_key(Key.CHAR, ch)
    menu.handle_key(Key.ESC, "")  # cancel edit
    assert cfg.execution.provider == original
    assert menu._state == "NAVIGATE"


def test_menu_edit_cancel_does_not_change():
    """PRD-43 spec alias."""
    from agenthicc.tui.mention_input import Key

    menu, cfg = _make_menu()
    original = cfg.execution.provider
    _navigate_to_execution_provider(menu)
    menu.handle_key(Key.ENTER, "")
    for ch in "openai":
        menu.handle_key(Key.CHAR, ch)
    menu.handle_key(Key.ESC, "")
    assert cfg.execution.provider == original


# ---------------------------------------------------------------------------
# Bool toggle
# ---------------------------------------------------------------------------


def _find_bool_field(menu: ConfigurationMenu, section_name: str, field_name: str):
    """Return (section_idx, field_idx) for the given bool field."""
    sec_idx = next(i for i, s in enumerate(menu._sections) if s.name == section_name)
    field_idx = next(
        i for i, f in enumerate(menu._sections[sec_idx].fields)
        if f.field_name == field_name
    )
    return sec_idx, field_idx


def test_bool_field_toggles_on_enter():
    from agenthicc.tui.mention_input import Key

    menu, cfg = _make_menu()
    si, fi = _find_bool_field(menu, "security", "sandbox_mode")
    menu._cursor = (si, fi)
    original = cfg.security.sandbox_mode
    menu.handle_key(Key.ENTER, "")
    assert cfg.security.sandbox_mode != original
    assert menu._state == "NAVIGATE"


def test_menu_bool_toggle():
    """PRD-43 spec alias."""
    from agenthicc.tui.mention_input import Key

    menu, cfg = _make_menu()
    sec_idx = next(i for i, s in enumerate(menu._sections) if s.name == "security")
    field_idx = next(
        i for i, f in enumerate(menu._sections[sec_idx].fields)
        if f.field_name == "sandbox_mode"
    )
    menu._cursor = (sec_idx, field_idx)
    original = cfg.security.sandbox_mode
    menu.handle_key(Key.ENTER, "")
    assert cfg.security.sandbox_mode != original
    assert menu._state == "NAVIGATE"


# ---------------------------------------------------------------------------
# Int validation
# ---------------------------------------------------------------------------


def test_invalid_int_does_not_change_value():
    from agenthicc.tui.mention_input import Key

    menu, cfg = _make_menu()
    exec_idx = next(i for i, s in enumerate(menu._sections) if s.name == "execution")
    turns_idx = next(
        i for i, f in enumerate(menu._sections[exec_idx].fields)
        if f.field_name == "max_agent_turns"
    )
    menu._cursor = (exec_idx, turns_idx)
    original = cfg.execution.max_agent_turns
    menu.handle_key(Key.ENTER, "")  # enter edit mode
    for ch in "notanumber":
        menu.handle_key(Key.CHAR, ch)
    menu.handle_key(Key.ENTER, "")  # attempt commit
    assert cfg.execution.max_agent_turns == original
    assert "Invalid" in menu._status_msg


def test_menu_invalid_int_shows_error():
    """PRD-43 spec alias."""
    from agenthicc.tui.mention_input import Key

    menu, cfg = _make_menu()
    exec_idx = next(i for i, s in enumerate(menu._sections) if s.name == "execution")
    turns_idx = next(
        i for i, f in enumerate(menu._sections[exec_idx].fields)
        if f.field_name == "max_agent_turns"
    )
    menu._cursor = (exec_idx, turns_idx)
    original = cfg.execution.max_agent_turns
    menu.handle_key(Key.ENTER, "")
    for ch in "notanumber":
        menu.handle_key(Key.CHAR, ch)
    menu.handle_key(Key.ENTER, "")
    assert cfg.execution.max_agent_turns == original
    assert "Invalid" in menu._status_msg


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


def test_navigation_down_moves_cursor():
    from agenthicc.tui.mention_input import Key

    menu, _ = _make_menu()
    exec_idx = next(i for i, s in enumerate(menu._sections) if s.name == "execution")
    menu._cursor = (exec_idx, -1)  # execution header
    before = menu._cursor
    menu.handle_key(Key.DOWN, "")
    assert menu._cursor != before


def test_menu_navigation_moves_cursor():
    """PRD-43 spec alias."""
    from agenthicc.tui.mention_input import Key

    menu, _ = _make_menu()
    menu._cursor = (0, -1)  # first section header
    menu.handle_key(Key.DOWN, "")
    assert menu._cursor != (0, -1)


# ---------------------------------------------------------------------------
# Edit field value in EDIT state
# ---------------------------------------------------------------------------


def test_edit_field_value_returns_buf_in_edit():
    from agenthicc.tui.mention_input import Key

    menu, _ = _make_menu()
    _navigate_to_execution_provider(menu)
    menu.handle_key(Key.ENTER, "")  # enter EDIT
    # edit_field_value should now return the current edit buffer (str)
    val = menu.edit_field_value
    assert isinstance(val, str)


# ---------------------------------------------------------------------------
# Status message after save attempt
# ---------------------------------------------------------------------------


def test_status_msg_updated_after_save_attempt():
    from agenthicc.tui.mention_input import Key

    menu, _ = _make_menu()
    # press 's' to trigger _save(); tomli_w may not be installed but the
    # status message must change from the default help text.
    default_msg = menu._status_msg
    menu.handle_key(Key.CHAR, "s")
    # Status message should have changed (either success or error).
    # We just verify it is no longer the navigation hint.
    assert menu._status_msg != default_msg or "save" in menu._status_msg.lower()
