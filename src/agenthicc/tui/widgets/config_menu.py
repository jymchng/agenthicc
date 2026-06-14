"""ConfigurationMenu — interactive TUI widget for editing AgenthiccConfig."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from agenthicc.tui.menu import MenuResult, MenuResultKind
from agenthicc.tui.terminal import Key

__all__ = ["ConfigurationMenu", "_build_sections", "Section", "FieldSpec"]


@dataclass
class FieldSpec:
    field_name: str
    field_type: type
    value: Any
    editable: bool
    default: Any = None
    changed: bool = False


@dataclass
class Section:
    name: str
    fields: list[FieldSpec]


# Section attribute names to include (in order)
_SECTION_ATTRS = ["execution", "memory", "security", "api", "plugins"]


def _build_sections(cfg: Any) -> list[Section]:
    """Build section/field descriptors from an AgenthiccConfig instance."""
    sections: list[Section] = []

    for attr in _SECTION_ATTRS:
        section_obj = getattr(cfg, attr, None)
        if section_obj is None:
            continue
        # Try to get defaults by instantiating the section class with no args
        section_cls = type(section_obj)
        try:
            default_obj = section_cls()
        except TypeError:
            default_obj = None

        fields: list[FieldSpec] = []
        # Use dataclasses.fields if available, else vars()
        if dataclasses.is_dataclass(section_obj):
            for f in dataclasses.fields(section_obj):
                val = getattr(section_obj, f.name)
                ft = type(val)
                editable = ft in (str, int, float, bool)
                if isinstance(val, (list, dict)):
                    editable = False
                default_val = getattr(default_obj, f.name) if default_obj is not None else val
                changed = (val != default_val)
                fields.append(FieldSpec(
                    field_name=f.name,
                    field_type=ft,
                    value=val,
                    editable=editable,
                    default=default_val,
                    changed=changed,
                ))
        else:
            for k, v in vars(section_obj).items():
                if k.startswith("_"):
                    continue
                ft = type(v)
                editable = ft in (str, int, float, bool)
                if isinstance(v, (list, dict)):
                    editable = False
                default_val = getattr(default_obj, k) if default_obj is not None else v
                changed = (v != default_val)
                fields.append(FieldSpec(
                    field_name=k,
                    field_type=ft,
                    value=v,
                    editable=editable,
                    default=default_val,
                    changed=changed,
                ))

        if fields:
            sections.append(Section(name=attr, fields=fields))

    return sections


class ConfigurationMenu:
    """Interactive configuration editor widget."""

    def __init__(self, config: Any, console: Any = None) -> None:
        self._config = config
        self._console = console
        self._sections = _build_sections(config)
        self._state = "NAVIGATE"
        # cursor: (section_idx, field_idx), field_idx=-1 means section header
        self._cursor: tuple[int, int] = (0, -1)
        self._edit_buf: list[str] = []
        self._edit_original: str = ""
        self._status_msg = "Press Enter to edit, 's' to save, Esc to close"

    @property
    def edit_field_value(self) -> str | None:
        if self._state == "EDIT":
            return "".join(self._edit_buf)
        return None

    def render(self, prompt_str: str = "", buf: list = None, prev: int = 0) -> int:
        return 0

    def handle_key(self, key: Any, ch: str = "") -> MenuResult:
        if self._state == "NAVIGATE":
            return self._handle_navigate(key, ch)
        else:
            return self._handle_edit(key, ch)

    def _handle_navigate(self, key: Any, ch: str) -> MenuResult:
        if key == Key.ESC or key == "ESC":
            return MenuResult.cancel()

        if key == Key.DOWN or key == "DOWN":
            self._move_cursor_down()
            return MenuResult.continue_()

        if key == Key.ENTER or key == "ENTER":
            field = self._current_field()
            if field is None or not field.editable:
                return MenuResult.continue_()
            if field.field_type is bool:
                # Toggle bool
                current_val = getattr(self._get_section_obj(), field.field_name)
                setattr(self._get_section_obj(), field.field_name, not current_val)
                field.value = not current_val
                field.changed = (field.value != field.default)
                return MenuResult.continue_()
            else:
                # Enter edit mode
                self._state = "EDIT"
                current_val = getattr(self._get_section_obj(), field.field_name)
                self._edit_original = str(current_val)
                self._edit_buf = list(str(current_val))
                return MenuResult.continue_()

        if (key == Key.CHAR or key == "CHAR") and ch == "s":
            self._save()
            return MenuResult.continue_()

        return MenuResult.continue_()

    def _handle_edit(self, key: Any, ch: str) -> MenuResult:
        if key == Key.ESC or key == "ESC":
            field = self._current_field()
            if field is not None:
                self._restore_field(field)
            self._state = "NAVIGATE"
            self._edit_buf = []
            return MenuResult.continue_()

        if key == Key.ENTER or key == "ENTER":
            field = self._current_field()
            if field is None:
                self._state = "NAVIGATE"
                return MenuResult.continue_()
            value_str = "".join(self._edit_buf)
            if field.field_type is int:
                try:
                    int_val = int(value_str)
                    setattr(self._get_section_obj(), field.field_name, int_val)
                    field.value = int_val
                    field.changed = (field.value != field.default)
                    self._state = "NAVIGATE"
                    self._save()
                    self._status_msg = f"Updated {field.field_name}"
                except ValueError:
                    self._status_msg = f"Invalid integer value for {field.field_name}"
                    self._state = "NAVIGATE"
            else:
                setattr(self._get_section_obj(), field.field_name, value_str)
                field.value = value_str
                field.changed = (field.value != field.default)
                self._state = "NAVIGATE"
                self._save()
                self._status_msg = f"Updated {field.field_name}"
            return MenuResult.continue_()

        if key == Key.BACKSPACE or key == "BACKSPACE":
            if self._edit_buf:
                self._edit_buf.pop()
            return MenuResult.continue_()

        if key == Key.CHAR or key == "CHAR":
            self._edit_buf.append(ch)
            return MenuResult.continue_()

        return MenuResult.continue_()

    def _current_field(self) -> FieldSpec | None:
        sec_idx, field_idx = self._cursor
        if field_idx < 0:
            return None
        if sec_idx >= len(self._sections):
            return None
        section = self._sections[sec_idx]
        if field_idx >= len(section.fields):
            return None
        return section.fields[field_idx]

    def _get_section_obj(self) -> Any:
        sec_idx, _ = self._cursor
        if sec_idx >= len(self._sections):
            return self._config
        section = self._sections[sec_idx]
        return getattr(self._config, section.name, self._config)

    def _move_cursor_down(self) -> None:
        sec_idx, field_idx = self._cursor
        if sec_idx >= len(self._sections):
            return
        section = self._sections[sec_idx]
        next_field = field_idx + 1
        if next_field < len(section.fields):
            self._cursor = (sec_idx, next_field)
        else:
            next_sec = sec_idx + 1
            if next_sec < len(self._sections):
                self._cursor = (next_sec, -1)
            else:
                self._cursor = (0, -1)

    def _restore_field(self, field: FieldSpec) -> None:
        original = self._edit_original
        if field.field_type is int:
            try:
                int_val = int(original)
                setattr(self._get_section_obj(), field.field_name, int_val)
                field.value = int_val
                field.changed = (field.value != field.default)
            except ValueError:
                pass
        else:
            setattr(self._get_section_obj(), field.field_name, original)
            field.value = original
            field.changed = (field.value != field.default)

    def _save(self) -> None:
        """Persist the current config to .agenthicc/agenthicc.toml."""
        import dataclasses as _dc  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415
        try:
            target = Path(".agenthicc/agenthicc.toml")
            target.parent.mkdir(parents=True, exist_ok=True)
            lines: list[str] = ["# agenthicc.toml — saved by /config menu\n"]
            for section_name in _SECTION_ATTRS:
                section_obj = getattr(self._config, section_name, None)
                if section_obj is None:
                    continue
                lines.append(f"\n[{section_name}]\n")
                if _dc.is_dataclass(section_obj):
                    for f in _dc.fields(section_obj):
                        val = getattr(section_obj, f.name)
                        if isinstance(val, (list, dict)):
                            continue
                        lines.append(f"{f.name} = {self._toml_val(val)}\n")
                else:
                    for k, v in vars(section_obj).items():
                        if k.startswith("_"):
                            continue
                        if isinstance(v, (list, dict)):
                            continue
                        lines.append(f"{k} = {self._toml_val(v)}\n")
            target.write_text("".join(lines), encoding="utf-8")
            self._status_msg = "Saved to .agenthicc/agenthicc.toml"
        except Exception as exc:  # noqa: BLE001
            self._status_msg = f"Save failed: {exc}"

    def _toml_val(self, val: Any) -> str:
        """Format a scalar value as a TOML literal."""
        if isinstance(val, bool):
            return "true" if val else "false"
        if isinstance(val, str):
            return repr(val)
        return str(val)

    # Compat: legacy methods
    def get_value(self, key: str) -> Any:
        return None

    def set_value(self, key: str, value: Any) -> None:
        pass
