"""Configuration Menu — live editing of AgenthiccConfig via /config (PRD-43)."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from agenthicc.tui.menu import MenuResult, MenuResultKind, MenuWidget  # noqa: F401

__all__ = [
    "ConfigField",
    "ConfigSection",
    "ConfigurationMenu",
    "_build_sections",
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ConfigField:
    section_name: str  # e.g. "execution"
    field_name: str  # e.g. "provider"
    label: str  # e.g. "provider"
    value: Any  # current live value
    default: Any  # default from the dataclass
    field_type: type  # int, str, bool, float
    editable: bool  # False for list/dict fields
    changed: bool = False


@dataclass
class ConfigSection:
    name: str  # e.g. "execution"
    fields: list[ConfigField]
    expanded: bool = True


def _build_sections(cfg: Any) -> list[ConfigSection]:
    """Walk AgenthiccConfig and build a list of ConfigSection objects."""
    SECTION_ATTRS = ["execution", "memory", "security", "api", "plugins"]
    sections: list[ConfigSection] = []

    for attr in SECTION_ATTRS:
        section_obj = getattr(cfg, attr, None)
        if section_obj is None:
            continue
        section_cls = type(section_obj)
        # Create a default instance for comparison; guard against constructors
        # that require arguments.
        try:
            default_obj = section_cls()
        except TypeError:
            default_obj = section_obj  # fall back: no default available

        cfg_fields: list[ConfigField] = []
        for f in dataclasses.fields(section_cls):
            val = getattr(section_obj, f.name)
            default_val = getattr(default_obj, f.name)
            # Determine editability: scalar types only
            editable = f.type in ("str", "int", "float", "bool") or f.type in (
                str,
                int,
                float,
                bool,
            )
            # Skip complex fields (list, dict) for v1
            if isinstance(val, (list, dict)):
                editable = False
            cfg_fields.append(
                ConfigField(
                    section_name=attr,
                    field_name=f.name,
                    label=f.name.replace("_", " "),
                    value=val,
                    default=default_val,
                    field_type=type(val),
                    editable=editable,
                    changed=(val != default_val),
                )
            )
        sections.append(ConfigSection(name=attr, fields=cfg_fields))

    return sections


# ---------------------------------------------------------------------------
# ConfigurationMenu
# ---------------------------------------------------------------------------


class ConfigurationMenu:
    """Interactive configuration editor.  Implements MenuWidget.

    States
    ------
    NAVIGATE  — cursor moves between section headers and fields;
                ``edit_field_value`` returns None.
    EDIT      — typing a new value; ``edit_field_value`` returns the pending
                string so the input bar can show it.
    """

    _MAX_VISIBLE = 12  # rows of the menu panel (excludes separators + status)

    def __init__(self, cfg: Any, console: Any) -> None:
        self._cfg = cfg
        self._console = console
        self._sections = _build_sections(cfg)
        self._cursor: tuple[int, int] = (0, -1)  # (section_idx, field_idx or -1)
        self._state: str = "NAVIGATE"  # "NAVIGATE" | "EDIT"
        self._edit_buf: str = ""
        self._status_msg: str = "↑↓ navigate   Enter edit/expand   Esc close"
        self._scroll_offset: int = 0

    # ── MenuWidget protocol ───────────────────────────────────────────────

    @property
    def edit_field_value(self) -> str | None:
        """Return pending edit buffer when in EDIT state, else None."""
        if self._state == "EDIT":
            return self._edit_buf
        return None

    def render(self, prompt_str: str, buf: list[str], prev_n_lines: int) -> int:
        """Erase old rows, redraw input line + menu panel, move cursor back up.

        Returns the number of lines rendered below the current terminal row so
        the caller can pass it back as *prev_n_lines* on the next call.
        """
        import shutil
        import sys

        out = sys.stdout
        cols = shutil.get_terminal_size((80, 24)).columns

        # 1. Erase old rows (move down then erase each line, then jump back up).
        if prev_n_lines > 0:
            for _ in range(prev_n_lines):
                out.write("\n\r\x1b[2K")
            out.write(f"\x1b[{prev_n_lines}A")

        # 2. Redraw input line.
        if self._state == "EDIT":
            fld = self._focused_field()
            if fld is not None:
                label = f"[editing: {fld.section_name}.{fld.field_name}]"
            else:
                label = "[editing]"
            out.write(f"\r\x1b[2K\x1b[2m{label}\x1b[0m {self._edit_buf}")
        else:
            out.write(f"\r\x1b[2K{prompt_str}{''.join(buf)}")

        # 3. Build all rows and select the visible window.
        rows = self._build_rows()
        visible = rows[self._scroll_offset : self._scroll_offset + self._MAX_VISIBLE]
        sep = "─" * min(cols - 2, 74)
        lines: list[str] = []
        lines.append(f"\r\x1b[2K  \x1b[2m{sep}\x1b[0m")
        lines.extend(f"\r\x1b[2K{row}" for row in visible)
        lines.append(f"\r\x1b[2K  \x1b[2m{sep}\x1b[0m")
        lines.append(f"\r\x1b[2K  \x1b[2m{self._status_msg}\x1b[0m")

        # 4. Write lines then move cursor back up to input row.
        n = len(lines)
        out.write("\n" + "\n".join(lines))
        out.write(f"\x1b[{n}A")
        # Reposition cursor at end of input content (cursor-up leaves it at
        # the last menu-row column, not the end of the input text).
        if self._state == "EDIT":
            out.write(f"\r\x1b[2m{label}\x1b[0m {self._edit_buf}")
        else:
            out.write(f"\r{prompt_str}{''.join(buf)}")
        out.flush()
        return n

    def handle_key(self, key: Any, ch: str) -> MenuResult:
        """Dispatch a keypress to the appropriate state handler."""
        from agenthicc.tui.mention_input import Key  # noqa: PLC0415 — lazy import

        if self._state == "EDIT":
            return self._handle_edit_key(key, ch)

        # ── NAVIGATE mode ──────────────────────────────────────────────
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

        return MenuResult.continue_()

    # ── Private helpers ───────────────────────────────────────────────────

    def _focused_field(self) -> ConfigField | None:
        """Return the ConfigField under the cursor, or None."""
        si, fi = self._cursor
        if si >= len(self._sections) or fi < 0:
            return None
        section = self._sections[si]
        if not section.expanded or fi >= len(section.fields):
            return None
        return section.fields[fi]

    def _handle_edit_key(self, key: Any, ch: str) -> MenuResult:
        """Handle a keypress while in EDIT state."""
        from agenthicc.tui.mention_input import Key  # noqa: PLC0415

        if key == Key.ESC:
            # Cancel the edit without applying anything.
            self._state = "NAVIGATE"
            self._edit_buf = ""
            self._status_msg = "↑↓ navigate   Enter edit/expand   Esc close"
        elif key == Key.ENTER:
            self._commit_edit()
            self._state = "NAVIGATE"
            self._edit_buf = ""
        elif key == Key.BACKSPACE:
            self._edit_buf = self._edit_buf[:-1]
        elif key == Key.CHAR and ch and ch.isprintable():
            self._edit_buf += ch

        return MenuResult.continue_()

    def _activate(self) -> None:
        """Expand/collapse a section header or enter edit mode on a field."""
        si, fi = self._cursor
        if fi == -1:
            # Cursor is on a section header — toggle expansion.
            self._sections[si].expanded = not self._sections[si].expanded
            return

        fld = self._focused_field()
        if fld is None or not fld.editable:
            return

        if fld.field_type is bool:
            # Toggle booleans immediately; no edit mode needed.
            new_val = not fld.value
            self._apply_value(fld, new_val)
            self._save()   # persist immediately
            self._status_msg = f"✓ {fld.section_name}.{fld.field_name} = {new_val!r}  (saved)"
        else:
            # Enter EDIT mode pre-populated with the current value.
            self._state = "EDIT"
            self._edit_buf = str(fld.value)
            self._status_msg = "Type new value   Enter save   Esc back to menu"

    def _commit_edit(self) -> None:
        """Parse _edit_buf, validate, apply to the live config, and persist."""
        fld = self._focused_field()
        if fld is None:
            return
        raw = self._edit_buf.strip()
        try:
            if fld.field_type is int:
                new_val: Any = int(raw)
            elif fld.field_type is float:
                new_val = float(raw)
            elif fld.field_type is bool:
                new_val = raw.lower() in ("true", "1", "yes")
            else:
                new_val = raw
            self._apply_value(fld, new_val)
            self._save()   # persist every confirmed edit to .agenthicc.toml
            self._status_msg = f"✓ {fld.section_name}.{fld.field_name} = {new_val!r}  (saved)"
        except (ValueError, TypeError) as exc:
            self._status_msg = f"✗ Invalid value: {exc}"

    def _apply_value(self, fld: ConfigField, new_val: Any) -> None:
        """Write *new_val* to the live config object and update the field model."""
        section_obj = getattr(self._cfg, fld.section_name)
        # Use object.__setattr__ to bypass frozen dataclass restrictions if any.
        object.__setattr__(section_obj, fld.field_name, new_val)
        fld.value = new_val
        fld.changed = new_val != fld.default

    def _move(self, delta: int) -> None:
        """Move the cursor by *delta* positions in the flat position list."""
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
        """Return a flat list of (section_idx, field_idx) cursor positions.

        Section headers are represented as (si, -1); expanded field rows as
        (si, fi) where fi >= 0.
        """
        pos: list[tuple[int, int]] = []
        for si, section in enumerate(self._sections):
            pos.append((si, -1))  # section header
            if section.expanded:
                for fi in range(len(section.fields)):
                    pos.append((si, fi))
        return pos

    def _adjust_scroll(self) -> None:
        """Keep the focused item within the _MAX_VISIBLE window."""
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
        """Collapse the section the cursor is in and move cursor to its header."""
        si, _fi = self._cursor
        self._sections[si].expanded = False
        self._cursor = (si, -1)

    def _build_rows(self) -> list[str]:
        """Build ANSI-formatted rows for the current sections state.

        Section headers:  ``  ▶/▼ section_name``  (bold + ► if focused)
        Field rows:       ``  ► label        ● value``  (reversed if focused,
                          yellow ● if changed)
        """
        rows: list[str] = []
        si_cur, fi_cur = self._cursor
        for si, section in enumerate(self._sections):
            is_sec_focused = si == si_cur and fi_cur == -1
            marker = "▼" if section.expanded else "▶"  # ▼ or ▶
            arrow = "►" if is_sec_focused else " "  # ► or space
            sec_line = f"  {arrow} {marker} {section.name}"
            rows.append(f"\x1b[1m{sec_line}\x1b[0m" if is_sec_focused else sec_line)

            if section.expanded:
                for fi, f in enumerate(section.fields):
                    is_focused = si == si_cur and fi == fi_cur
                    changed_marker = (
                        "\x1b[33m●\x1b[0m" if f.changed else " "
                    )  # yellow ● or space
                    if f.editable:
                        value_str = str(f.value)
                    else:
                        value_str = f"[{type(f.value).__name__}]"
                    field_arrow = "►" if is_focused else " "  # ► or space
                    row = f"  {field_arrow} {f.label:<28} {changed_marker} {value_str}"
                    rows.append(f"\x1b[7m{row}\x1b[0m" if is_focused else row)
        return rows

    @staticmethod
    def _to_toml_value(v: Any) -> str:
        """Serialise a scalar value to its TOML representation (stdlib only)."""
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        # String — escape backslashes and double-quotes
        escaped = str(v).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _save(self) -> None:
        """Persist the current config to .agenthicc/agenthicc.toml (stdlib only)."""
        from pathlib import Path  # noqa: PLC0415
        try:
            target = Path(".agenthicc/agenthicc.toml")
            target.parent.mkdir(parents=True, exist_ok=True)

            lines: list[str] = [
                "# agenthicc.toml — saved by /config menu\n"
            ]
            for section_name in ["execution", "memory", "security", "api", "plugins"]:
                section_obj = getattr(self._cfg, section_name, None)
                if section_obj is None:
                    continue
                lines.append(f"\n[{section_name}]\n")
                for f in dataclasses.fields(type(section_obj)):
                    val = getattr(section_obj, f.name)
                    if isinstance(val, (list, dict)):
                        continue  # skip complex fields for v1
                    lines.append(f"{f.name} = {self._to_toml_value(val)}\n")

            target.write_text("".join(lines), encoding="utf-8")
            self._status_msg = "✓ Saved to .agenthicc/agenthicc.toml"
        except Exception as exc:
            self._status_msg = f"✗ Save failed: {exc}"
