---
title: "PRD-43: Configuration Menu — Live Editing of AgenthiccConfig via /config"
status: draft
version: 0.1.0
created: 2026-06-13
depends-on: prd-41-menu-widget-system.md, prd-42-command-menu-dispatch.md
---

# PRD-43: Configuration Menu

## Executive Summary

`/config` opens an interactive `ConfigurationMenu` below the input bar.  The
user can navigate through sections and fields, edit values inline using the
input bar as an edit field, and see changes take effect immediately in the
running session.  Changes are optionally persisted to `.agenthicc/agenthicc.toml`.

---

## Goals

| ID | Goal |
|----|------|
| G1 | `/config` opens the menu; all config sections are listed |
| G2 | ↑/↓ navigate sections and fields |
| G3 | Tab / → expands a collapsed section; ← collapses it |
| G4 | Enter on a field enters **edit mode**: input bar shows current value for editing |
| G5 | Enter in edit mode confirms the new value; Esc cancels the edit |
| G6 | Type-validated: int fields reject non-numeric input; bool fields toggle on Enter |
| G7 | Changed values are applied to the live `AgenthiccConfig` object immediately |
| G8 | `s` (save) writes the current config to `.agenthicc/agenthicc.toml` |
| G9 | Esc at the top level closes the menu |
| G10 | Changed fields are visually marked with `●` until saved |

## Non-Goals
- List/dict field editing (show read-only for v1)
- Undo history
- Multi-level nested sections beyond the current depth

---

## Visual Design

```
❯ [editing: execution.model]   ← input bar, shows current value when editing
  ──────────────────────────────────────────────────────────────────────────
  ▼ execution
  │   provider         anthropic
  │ ► model          ● claude-sonnet-4-6    ← focused, changed
  │   max_agent_turns  200
  │   max_concurrent_intents  8
  ▶ memory                                  ← collapsed section
  ▶ security
  ▶ plugins
  ──────────────────────────────────────────────────────────────────────────
  ↑↓ navigate   Enter edit/expand   Esc cancel   s save
```

States:
- **NAVIGATE** — moving between sections and fields; `edit_field_value` returns None
- **EDIT** — typing a new value; `edit_field_value` returns the field's pending string
- **BOOL_TOGGLE** — bool fields toggle immediately, no edit mode needed

---

## Data Model

```python
# src/agenthicc/tui/widgets/config_menu.py

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field, fields
from typing import Any

from agenthicc.tui.menu import MenuWidget, MenuResult, MenuResultKind


@dataclass
class ConfigField:
    section_name: str    # e.g. "execution"
    field_name: str      # e.g. "provider"
    label: str           # e.g. "provider"
    value: Any           # current live value
    default: Any         # default from the dataclass
    field_type: type     # int, str, bool, float
    editable: bool       # False for list/dict fields
    changed: bool = False


@dataclass
class ConfigSection:
    name: str            # e.g. "execution"
    fields: list[ConfigField]
    expanded: bool = True


def _build_sections(cfg: Any) -> list[ConfigSection]:
    """Walk AgenthiccConfig and build a list of ConfigSection objects."""
    import agenthicc.config as _cfg_mod  # noqa: PLC0415

    SECTION_ATTRS = ["execution", "memory", "security", "api", "plugins"]
    sections: list[ConfigSection] = []

    for attr in SECTION_ATTRS:
        section_obj = getattr(cfg, attr, None)
        if section_obj is None:
            continue
        section_cls = type(section_obj)
        default_obj = section_cls()   # default instance for comparison
        cfg_fields: list[ConfigField] = []
        for f in dataclasses.fields(section_cls):
            val = getattr(section_obj, f.name)
            default_val = getattr(default_obj, f.name)
            editable = f.type in ("str", "int", "float", "bool") or f.type in (str, int, float, bool)
            # Skip complex fields (list, dict) for v1
            if isinstance(val, (list, dict)):
                editable = False
            cfg_fields.append(ConfigField(
                section_name=attr,
                field_name=f.name,
                label=f.name.replace("_", " "),
                value=val,
                default=default_val,
                field_type=type(val),
                editable=editable,
                changed=(val != default_val),
            ))
        sections.append(ConfigSection(name=attr, fields=cfg_fields))

    return sections
```

---

## `ConfigurationMenu` Class

```python
class ConfigurationMenu:
    """Interactive configuration editor.  Implements MenuWidget."""

    _MAX_VISIBLE = 12    # rows of the menu panel

    def __init__(self, cfg: Any, console: Any) -> None:
        self._cfg = cfg
        self._console = console
        self._sections = _build_sections(cfg)
        self._cursor: tuple[int, int] = (0, 0)   # (section_idx, field_idx or -1)
        self._state: str = "NAVIGATE"             # "NAVIGATE" | "EDIT"
        self._edit_buf: str = ""
        self._status_msg: str = "↑↓ navigate   Enter edit/expand   Esc close   s save"
        self._scroll_offset: int = 0

    # ── MenuWidget protocol ───────────────────────────────────────────────

    @property
    def edit_field_value(self) -> str | None:
        if self._state == "EDIT":
            return self._edit_buf
        return None

    def render(self, prompt_str: str, buf: list[str], prev_n_lines: int) -> int:
        import sys, shutil  # noqa: PLC0415
        out = sys.stdout
        cols = shutil.get_terminal_size((80, 24)).columns

        # Erase old rows
        if prev_n_lines > 0:
            for _ in range(prev_n_lines):
                out.write("\n\r\x1b[2K")
            out.write(f"\x1b[{prev_n_lines}A")

        # Redraw input line
        if self._state == "EDIT":
            field = self._focused_field()
            label = f"[editing: {field.section_name}.{field.field_name}]" if field else "[editing]"
            out.write(f"\r\x1b[2K\x1b[2m{label}\x1b[0m")
        else:
            out.write(f"\r\x1b[2K{prompt_str}{''.join(buf)}")

        # Build all visible rows
        rows = self._build_rows()
        visible = rows[self._scroll_offset: self._scroll_offset + self._MAX_VISIBLE]
        sep = "─" * min(cols - 2, 74)
        lines = [f"\r\x1b[2K  \x1b[2m{sep}\x1b[0m"]
        lines.extend(f"\r\x1b[2K{row}" for row in visible)
        lines.append(f"\r\x1b[2K  \x1b[2m{sep}\x1b[0m")
        lines.append(f"\r\x1b[2K  \x1b[2m{self._status_msg}\x1b[0m")

        n = len(lines)
        out.write("\n" + "\n".join(lines))
        out.write(f"\x1b[{n}A")
        out.flush()
        return n

    def handle_key(self, key: Any, ch: str) -> MenuResult:
        from agenthicc.tui.mention_input import Key  # noqa: PLC0415

        if self._state == "EDIT":
            return self._handle_edit_key(key, ch)

        # NAVIGATE
        if key == Key.ESC:
            return MenuResult.cancel()

        if key == Key.UP:
            self._move(-1)
        elif key == Key.DOWN:
            self._move(1)
        elif key in (Key.ENTER, Key.RIGHT):
            self._activate()
        elif key == Key.LEFT:
            self._collapse_section()
        elif key == Key.CHAR and ch == "s":
            self._save()
            self._status_msg = "✓ Saved to .agenthicc/agenthicc.toml"

        return MenuResult.continue_()

    # ── Private helpers ───────────────────────────────────────────────────

    def _focused_field(self) -> ConfigField | None:
        si, fi = self._cursor
        if si >= len(self._sections) or fi < 0:
            return None
        section = self._sections[si]
        if not section.expanded or fi >= len(section.fields):
            return None
        return section.fields[fi]

    def _handle_edit_key(self, key: Any, ch: str) -> MenuResult:
        from agenthicc.tui.mention_input import Key  # noqa: PLC0415
        if key == Key.ESC:
            self._state = "NAVIGATE"
            self._edit_buf = ""
        elif key in (Key.ENTER,):
            self._commit_edit()
            self._state = "NAVIGATE"
            self._edit_buf = ""
        elif key == Key.BACKSPACE:
            self._edit_buf = self._edit_buf[:-1]
        elif key == Key.CHAR and ch and ch.isprintable():
            self._edit_buf += ch
        return MenuResult.continue_()

    def _activate(self) -> None:
        si, fi = self._cursor
        if fi == -1:
            # Cursor on a section header → expand/collapse
            self._sections[si].expanded = not self._sections[si].expanded
            return
        field = self._focused_field()
        if field is None or not field.editable:
            return
        if field.field_type is bool:
            # Toggle immediately
            new_val = not field.value
            self._apply_value(field, new_val)
        else:
            self._state = "EDIT"
            self._edit_buf = str(field.value)

    def _commit_edit(self) -> None:
        field = self._focused_field()
        if field is None:
            return
        raw = self._edit_buf.strip()
        try:
            if field.field_type is int:
                new_val = int(raw)
            elif field.field_type is float:
                new_val = float(raw)
            elif field.field_type is bool:
                new_val = raw.lower() in ("true", "1", "yes")
            else:
                new_val = raw
            self._apply_value(field, new_val)
            self._status_msg = f"✓ {field.section_name}.{field.field_name} = {new_val!r}"
        except (ValueError, TypeError) as exc:
            self._status_msg = f"✗ Invalid value: {exc}"

    def _apply_value(self, field: ConfigField, new_val: Any) -> None:
        """Apply a new value to the live config object."""
        section_obj = getattr(self._cfg, field.section_name)
        object.__setattr__(section_obj, field.field_name, new_val)
        field.value = new_val
        field.changed = new_val != field.default

    def _move(self, delta: int) -> None:
        # Build a flat list of cursor positions, then move
        positions = self._all_positions()
        if not positions:
            return
        try:
            idx = positions.index(self._cursor)
        except ValueError:
            idx = 0
        self._cursor = positions[(idx + delta) % len(positions)]
        self._adjust_scroll()

    def _all_positions(self) -> list[tuple[int, int]]:
        pos = []
        for si, section in enumerate(self._sections):
            pos.append((si, -1))   # section header
            if section.expanded:
                for fi in range(len(section.fields)):
                    pos.append((si, fi))
        return pos

    def _adjust_scroll(self) -> None:
        flat = self._all_positions()
        try:
            idx = flat.index(self._cursor)
        except ValueError:
            return
        if idx < self._scroll_offset:
            self._scroll_offset = idx
        elif idx >= self._scroll_offset + self._MAX_VISIBLE:
            self._scroll_offset = idx - self._MAX_VISIBLE + 1

    def _collapse_section(self) -> None:
        si, fi = self._cursor
        self._sections[si].expanded = False
        self._cursor = (si, -1)

    def _build_rows(self) -> list[str]:
        rows = []
        si_cur, fi_cur = self._cursor
        for si, section in enumerate(self._sections):
            is_sec_focused = (si == si_cur and fi_cur == -1)
            marker = "▼" if section.expanded else "▶"
            sec_line = f"  {'►' if is_sec_focused else ' '} {marker} {section.name}"
            rows.append(f"\x1b[1m{sec_line}\x1b[0m" if is_sec_focused else sec_line)
            if section.expanded:
                for fi, f in enumerate(section.fields):
                    is_focused = (si == si_cur and fi == fi_cur)
                    changed_marker = "\x1b[33m●\x1b[0m" if f.changed else " "
                    value_str = str(f.value) if f.editable else f"[{type(f.value).__name__}]"
                    row = f"  {'►' if is_focused else ' '} {f.label:<28} {changed_marker} {value_str}"
                    rows.append(f"\x1b[7m{row}\x1b[0m" if is_focused else row)
        return rows

    def _save(self) -> None:
        """Persist current config to .agenthicc/agenthicc.toml."""
        try:
            import tomllib, tomli_w  # noqa: PLC0415 — optional deps
            from pathlib import Path  # noqa: PLC0415
            target = Path(".agenthicc/agenthicc.toml")
            target.parent.mkdir(parents=True, exist_ok=True)
            # Build a dict from current config
            data = {}
            for section_name in ["execution", "memory", "security", "api", "plugins"]:
                section_obj = getattr(self._cfg, section_name, None)
                if section_obj is None:
                    continue
                data[section_name] = {
                    f.name: getattr(section_obj, f.name)
                    for f in dataclasses.fields(type(section_obj))
                    if not isinstance(getattr(section_obj, f.name), (list, dict))
                }
            target.write_bytes(tomli_w.dumps(data).encode())
        except ImportError:
            self._status_msg = "✗ Save requires tomli-w: pip install tomli-w"
        except Exception as exc:
            self._status_msg = f"✗ Save failed: {exc}"
```

---

## Tests

```python
# tests/unit/test_config_menu.py

import pytest
from unittest.mock import MagicMock, patch
from agenthicc.tui.menu import MenuResultKind
from agenthicc.tui.widgets.config_menu import ConfigurationMenu, _build_sections
from agenthicc.config import AgenthiccConfig

pytestmark = pytest.mark.unit


def _make_menu():
    cfg = AgenthiccConfig()
    return ConfigurationMenu(cfg, MagicMock()), cfg


def test_build_sections_includes_execution():
    cfg = AgenthiccConfig()
    sections = _build_sections(cfg)
    names = [s.name for s in sections]
    assert "execution" in names


def test_build_sections_fields_have_correct_types():
    cfg = AgenthiccConfig()
    sections = _build_sections(cfg)
    exec_section = next(s for s in sections if s.name == "execution")
    provider_field = next(f for f in exec_section.fields if f.field_name == "provider")
    assert provider_field.field_type is str
    assert provider_field.value == "anthropic"


def test_menu_esc_returns_cancel():
    from agenthicc.tui.mention_input import Key
    menu, _ = _make_menu()
    result = menu.handle_key(Key.ESC, "")
    assert result.kind == MenuResultKind.CANCEL


def test_menu_edit_mode_on_enter():
    from agenthicc.tui.mention_input import Key
    menu, _ = _make_menu()
    # Navigate to execution.provider field (si=0, fi=0)
    menu._cursor = (0, 0)   # execution, provider
    menu.handle_key(Key.ENTER, "")
    assert menu._state == "EDIT"
    assert menu.edit_field_value is not None


def test_menu_edit_commits_value():
    from agenthicc.tui.mention_input import Key
    menu, cfg = _make_menu()
    menu._cursor = (0, 0)  # execution.provider
    menu.handle_key(Key.ENTER, "")  # enter edit mode
    for ch in "openai":
        menu.handle_key(Key.CHAR, ch)
    menu.handle_key(Key.ENTER, "")  # confirm
    assert cfg.execution.provider == "openai"
    assert menu._state == "NAVIGATE"


def test_menu_edit_cancel_does_not_change():
    from agenthicc.tui.mention_input import Key
    menu, cfg = _make_menu()
    original = cfg.execution.provider
    menu._cursor = (0, 0)
    menu.handle_key(Key.ENTER, "")
    for ch in "openai":
        menu.handle_key(Key.CHAR, ch)
    menu.handle_key(Key.ESC, "")  # cancel edit
    assert cfg.execution.provider == original


def test_menu_bool_toggle():
    from agenthicc.tui.mention_input import Key
    menu, cfg = _make_menu()
    # Find sandbox_mode (bool) in security section
    sec_idx = next(i for i, s in enumerate(menu._sections) if s.name == "security")
    field_idx = next(
        i for i, f in enumerate(menu._sections[sec_idx].fields)
        if f.field_name == "sandbox_mode"
    )
    menu._cursor = (sec_idx, field_idx)
    original = cfg.security.sandbox_mode
    menu.handle_key(Key.ENTER, "")  # toggle
    assert cfg.security.sandbox_mode != original
    assert menu._state == "NAVIGATE"  # no edit mode for bool


def test_menu_navigation_moves_cursor():
    from agenthicc.tui.mention_input import Key
    menu, _ = _make_menu()
    menu._cursor = (0, -1)   # execution header
    menu.handle_key(Key.DOWN, "")
    assert menu._cursor != (0, -1)


def test_menu_edit_field_value_none_in_navigate():
    menu, _ = _make_menu()
    assert menu.edit_field_value is None


def test_menu_invalid_int_shows_error():
    from agenthicc.tui.mention_input import Key
    menu, cfg = _make_menu()
    # Find max_agent_turns (int)
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
    assert cfg.execution.max_agent_turns == original   # unchanged
    assert "Invalid" in menu._status_msg
```

---

## Verification

```bash
PYTHONPATH=src .venv/bin/pytest tests/unit/test_menu_system.py \
                                 tests/unit/test_command_menu_dispatch.py \
                                 tests/unit/test_config_menu.py -v

uv run agenthicc
# Type /config and press Enter
# → ConfigurationMenu opens below the input bar
# → ↓↓ navigate to execution.model
# → Enter → input bar shows current model for editing
# → Type new model name → Enter
# → Model updated in live session immediately
# → s → saved to .agenthicc/agenthicc.toml
# → Esc → menu closes, normal input resumes
```
