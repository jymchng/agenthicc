# PRD-65 — Mode System & Configuration Overlay

## 1. Mode System

### 1.1 What Modes Are

Modes are **named execution contexts** that alter how the agent interprets
messages. The current implementation has an `Auto` mode by default; plugins and
skills can contribute additional modes. The user cycles through modes with
`Shift+Tab`. The active mode is displayed in the footer and passed to the agent
as context.

Modes are **not UI screens**. Switching a mode does not navigate anywhere.
It changes a single reactive signal which propagates to the footer display and
to the agent context at the next turn.

### 1.2 Data Model

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Mode:
    """A named execution context for the agent."""
    name: str          # e.g. "Auto", "Code", "Ask"
    badge: str         # e.g. "⏵⏵", "⌨", "?"
    description: str   # shown in picker
    system_prompt_suffix: str = ""  # appended to system prompt when active


class ModeRegistry:
    """Ordered list of available modes. Plugins contribute here."""

    def __init__(self) -> None:
        self._modes: list[Mode] = []

    def register(self, mode: Mode) -> None:
        self._modes.append(mode)

    def all(self) -> list[Mode]:
        return list(self._modes)

    def get(self, name: str) -> Mode | None:
        return next((m for m in self._modes if m.name == name), None)


def build_default_registry() -> ModeRegistry:
    """Default modes shipped with agenthicc."""
    reg = ModeRegistry()
    reg.register(Mode(
        name="Auto",
        badge="⏵⏵",
        description="Automatic — agent chooses approach",
    ))
    # Skills and plugins add more modes via PluginRegistry.register_mode()
    return reg
```

### 1.3 ModeManager

```python
class ModeManager:
    """Manages the active mode and handles cycling."""

    def __init__(self, registry: ModeRegistry) -> None:
        self._registry = registry
        self._idx = 0

    @property
    def active(self) -> Mode:
        modes = self._registry.all()
        return modes[self._idx % len(modes)]

    def cycle(self) -> Mode:
        """Advance to the next mode and return it."""
        modes = self._registry.all()
        self._idx = (self._idx + 1) % len(modes)
        return self.active

    def set_by_name(self, name: str) -> Mode | None:
        modes = self._registry.all()
        for i, m in enumerate(modes):
            if m.name == name:
                self._idx = i
                return m
        return None
```

### 1.4 Reactive Integration

`ModeManager` is owned by `UnifiedInputSession`. On `Shift+Tab`:

```python
# In UnifiedInputSession._dispatch_idle:
case Key.SHIFT_TAB:
    new_mode = self._mode_manager.cycle()
    self._mode_notification = new_mode  # shown in footer next render
    # Update ConversationStore so footer and agent context both see it
    self._state.conversation.mode_str.set(self._build_mode_str())
    self._state.conversation.active_mode_name.set(new_mode.name)
    self._state.conversation.active_mode_badge.set(new_mode.badge)
```

Add to `ConversationStore` (PRD-59 §3 extension):

```python
# In ConversationStore.__init__:
self.active_mode_name:  Signal[str] = Signal("Auto")
self.active_mode_badge: Signal[str] = Signal("⏵⏵")
self.mode_str:          Signal[str] = Signal(
    "⏵⏵ Auto  (shift+tab to cycle)  │  ctrl+j = ↵"
)
self.notification:      Signal[str | None] = Signal(None)  # transient footer text
```

### 1.5 Mode String Formatting

```python
_NEW_LINE_HINT = "  │  ctrl+j = ↵"

def build_mode_str(mode: Mode) -> str:
    if mode.name == "Auto":
        return f"{mode.badge} Auto  (shift+tab to cycle){_NEW_LINE_HINT}"
    return f"{mode.badge} {mode.name}  (shift+tab to cycle){_NEW_LINE_HINT}"
```

### 1.6 Mode Notification in Footer

When the user cycles modes, the footer temporarily shows `❖ Switched to X mode`
instead of the hints. This is driven by `ConversationStore.notification`.

```python
# In UnifiedInputSession._dispatch_idle, Shift+Tab case:
new_mode = self._mode_manager.cycle()
mode_str = build_mode_str(new_mode)
self._state.conversation.mode_str.set(mode_str)
# Show notification for 2 seconds, then clear
self._state.conversation.notification.set(f"❖ Switched to {new_mode.name} mode")
asyncio.get_event_loop().call_later(
    2.0,
    lambda: self._state.conversation.notification.set(None)
)
```

`FooterComponent` checks notification first:

```python
def _hints_line(self, cols: int) -> str:
    notif = self._state.conversation.notification()
    if notif:
        return fit(f"[dim]{notif}[/dim]", cols)
    ...
```

### 1.7 Mode Passed to Agent Context

```python
# In AgentRuntime.handle_send_message:
active_mode = self._mode_manager.active
context = AgentContext(
    text=cmd.text,
    mode=active_mode.name,
    mode_suffix=active_mode.system_prompt_suffix,
    session_id=self._conv.session_id(),
    history=self._conv.turns(),
)
```

### 1.8 Plugin Mode Registration

```python
# In PluginRegistry (PRD-63):
def register_mode(self, mode: Mode) -> None:
    self._mode_registry.register(mode)
```

Example plugin:

```python
class CodePlugin:
    def register(self, registry: PluginRegistry) -> None:
        registry.register_mode(Mode(
            name="Code",
            badge="⌨",
            description="Focused on code generation and analysis",
            system_prompt_suffix="Focus on code. Be concise. Prefer code over prose.",
        ))
```

---

## 2. UnifiedInputSession — Complete Idle Dispatch

This section fills the gap from PRD-62 §2 where `_dispatch_idle` was left
as "existing IdleInputSession logic." Here is the complete specification.

### 2.1 Full Key Dispatch (Idle Mode)

```python
async def _dispatch_idle(self, key: Key, ch: str) -> None:
    # ── interrupt ──────────────────────────────────────────────────────────
    match key:
        case Key.CTRL_C:
            self._ctrl_c_sequence()
            return

        case Key.CTRL_D:
            text = self._buf.text
            if text:
                await self._submit_text(text)
            else:
                await self._exit()
            return

    # ── paste ──────────────────────────────────────────────────────────────
    if key == Key.PASTE and ch:
        self._paste.apply(self._buf, ch, _get_cols())
        self._push()
        return

    if key == Key.CTRL_V:
        self._paste.expand()
        self._push()
        return

    # ── trigger detection ──────────────────────────────────────────────────
    if self._is_trigger_char(key, ch):
        trigger_char = "@" if key == Key.AT else ch
        handler = self._registry.get(trigger_char) if self._registry else None
        if handler and handler.can_activate(self._buf.buf[:self._buf.cursor]):
            await self._overlay_host.show_trigger_picker(
                initial_buf=list(self._buf.buf) + [trigger_char],
                registry=self._registry,
                cwd=self._cwd,
                on_complete=self._on_trigger_complete,
            )
            return
        # Not activatable → insert as literal
        self._paste_exit()
        self._buf.insert(trigger_char)
        self._push()
        return

    # ── main dispatch ──────────────────────────────────────────────────────
    match key:
        case Key.ENTER:
            text = self._buf.text.strip()
            if self._paste.condensed:
                text = self._buf.text.strip()
            if text:
                self._buf.clear()
                self._paste.condensed = False
                self._push()
                self._hist.commit(text)
                await self._cmd_bus.dispatch_async(SendMessageCommand(text=text))

        case Key.CTRL_ENTER:    # multi-line newline
            self._paste_exit()
            self._buf.insert("\n")
            self._push()

        case Key.BACKSPACE:
            if self._paste.condensed:
                self._paste.backspace(self._buf)
            elif self._buf.cursor == len(self._buf):
                tail = self._find_trigger_tail()
                if tail:
                    tch, tpre, tfrag = tail
                    handler = self._registry.get(tch) if self._registry else None
                    if handler:
                        self._buf.set(tpre)
                        await self._overlay_host.show_trigger_picker(
                            initial_buf=list(tpre) + [tch] + list(tfrag),
                            registry=self._registry,
                            cwd=self._cwd,
                            on_complete=self._on_trigger_complete,
                        )
                        return
                self._buf.delete_before()
            else:
                self._buf.delete_before()
            self._push()

        case Key.CTRL_U:
            self._buf.clear()
            self._paste.condensed = False
            self._push()

        case Key.LEFT:
            self._paste_exit()
            self._buf.move_left()
            self._push()

        case Key.RIGHT:
            self._paste_exit()
            self._buf.move_right()
            self._push()

        case Key.HOME:
            self._paste_exit()
            self._buf.move_home()
            self._push()

        case Key.END:
            self._paste_exit()
            self._buf.move_end()
            self._push()

        case Key.UP:
            self._paste_exit()
            if not self._buf.move_up():
                result = self._hist.up(self._buf.buf)
                if result is not None:
                    self._buf.set(result)
                    self._paste.condensed = False
            self._push()

        case Key.DOWN:
            self._paste_exit()
            if not self._buf.move_down():
                result = self._hist.down(self._buf.buf)
                if result is not None:
                    self._buf.set(result)
                    self._paste.condensed = False
            self._push()

        case Key.SHIFT_TAB:
            new_mode = self._mode_manager.cycle()
            self._state.conversation.mode_str.set(build_mode_str(new_mode))
            self._state.conversation.active_mode_name.set(new_mode.name)
            self._state.conversation.notification.set(
                f"❖ Switched to {new_mode.name} mode"
            )
            asyncio.get_event_loop().call_later(
                2.0,
                lambda: self._state.conversation.notification.set(None),
            )

        case Key.CHAR if ch:
            self._paste_exit()
            # Re-enter trigger mode when typing into an existing token
            if not ch.isspace() and self._buf.cursor == len(self._buf):
                tail = self._find_trigger_tail()
                if tail:
                    tch, tpre, tfrag = tail
                    handler = self._registry.get(tch) if self._registry else None
                    if handler:
                        self._buf.set(tpre)
                        await self._overlay_host.show_trigger_picker(
                            initial_buf=list(tpre) + [tch] + list(tfrag) + [ch],
                            registry=self._registry,
                            cwd=self._cwd,
                            on_complete=self._on_trigger_complete,
                        )
                        return
            if ch in (self._registry.chars if self._registry else set()):
                self._activate_trigger_char(ch)
            else:
                self._buf.insert(ch)
            self._push()
```

---

## 3. Configuration Overlay (ConfigMenuOverlay)

### 3.1 Purpose

`ConfigMenuOverlay` is the full-featured configuration editor that opens when the
user runs `/config`. It replaces the current `ConfigurationMenu` widget. It renders
inside the `OverlayHost` as part of the always-on Live region.

### 3.2 States

```python
class ConfigMenuState(Enum):
    NAVIGATE = auto()   # cursor moves between sections and fields
    EDIT     = auto()   # user is typing a new value for a field
```

### 3.3 Data Model

```python
@dataclass
class ConfigField:
    section_name: str
    field_name: str
    label: str
    value: Any
    default: Any
    field_type: type
    editable: bool
    changed: bool = False


@dataclass
class ConfigSection:
    name: str
    fields: list[ConfigField]
    expanded: bool = True
```

### 3.4 Overlay Implementation

```python
class ConfigMenuOverlay(Overlay):
    name = "config"
    _MAX_VISIBLE = 12

    def __init__(self, cfg: Any, on_close: Callable) -> None:
        self._cfg       = cfg
        self._on_close  = on_close
        self._sections  = _build_sections(cfg)
        self._cursor    = (0, -1)   # (section_idx, field_idx; -1 = header)
        self._state     = ConfigMenuState.NAVIGATE
        self._edit_buf  = ""
        self._scroll    = 0
        self._status    = "↑↓ navigate   Enter edit/expand   s save   Esc close"

    def render(self) -> Any:
        from rich.console import Group
        from rich.text import Text
        rows = self._build_rows()
        visible = rows[self._scroll : self._scroll + self._MAX_VISIBLE]
        sep = Text("─" * 60, style="dim")

        lines = [sep]
        for row in visible:
            lines.append(Text.from_markup(row))
        lines += [sep, Text.from_markup(f"  [dim]{self._status}[/dim]")]
        return Group(*lines)

    def handle_key(self, key: Key, ch: str) -> bool:
        from agenthicc.tui.cbreak_reader import Key
        if self._state == ConfigMenuState.EDIT:
            return self._handle_edit(key, ch)

        match key:
            case Key.ESC:
                self._on_close()
            case Key.UP:
                self._move(-1)
            case Key.DOWN:
                self._move(1)
            case Key.ENTER | Key.RIGHT:
                self._activate()
            case Key.LEFT:
                self._collapse()
            case Key.CHAR if ch == "s":
                self._save()
        return True

    def _handle_edit(self, key: Key, ch: str) -> bool:
        from agenthicc.tui.cbreak_reader import Key
        match key:
            case Key.ESC:
                self._state = ConfigMenuState.NAVIGATE
                self._edit_buf = ""
            case Key.ENTER:
                self._commit_edit()
                self._state = ConfigMenuState.NAVIGATE
                self._edit_buf = ""
            case Key.BACKSPACE:
                self._edit_buf = self._edit_buf[:-1]
            case Key.CHAR if ch and ch.isprintable():
                self._edit_buf += ch
        return True

    def _build_rows(self) -> list[str]:
        if not self._sections:
            return [
                "  [dim](no configuration loaded)[/dim]",
                "  [dim]Start agenthicc from a project directory[/dim]",
                "  [dim]with an agenthicc.toml file.  Esc to close.[/dim]",
            ]
        rows: list[str] = []
        si_cur, fi_cur = self._cursor
        for si, section in enumerate(self._sections):
            icon = "▼" if section.expanded else "▶"
            focused = (si == si_cur and fi_cur == -1)
            header = f"  {'[reverse]' if focused else ''}[bold]{icon} {section.name}[/bold]{'[/reverse]' if focused else ''}"
            rows.append(header)
            if section.expanded:
                for fi, field in enumerate(section.fields):
                    focused = (si == si_cur and fi == fi_cur)
                    indicator = "▶" if focused else " "
                    changed = "[yellow]●[/yellow] " if field.changed else "  "
                    val = str(field.value)[:30]
                    if focused and self._state == ConfigMenuState.EDIT:
                        val = self._edit_buf + "█"
                    row = f"  {'[reverse]' if focused else ''}{indicator} {changed}{field.label:<24}{val}{'[/reverse]' if focused else ''}"
                    rows.append(row)
        return rows

    def _activate(self) -> None:
        si, fi = self._cursor
        if fi == -1:
            self._sections[si].expanded = not self._sections[si].expanded
            return
        field = self._focused_field()
        if field and field.editable:
            self._edit_buf = str(field.value)
            self._state = ConfigMenuState.EDIT

    def _commit_edit(self) -> None:
        field = self._focused_field()
        if not field:
            return
        try:
            new_val = field.field_type(self._edit_buf)
            object.__setattr__(field, "value", new_val)
            object.__setattr__(field, "changed", True)
        except (ValueError, TypeError):
            self._status = f"[red]Invalid value for {field.field_type.__name__}[/red]"

    def _save(self) -> None:
        if self._cfg is None:
            self._status = "[red]No config loaded[/red]"
            return
        for section in self._sections:
            for field in section.fields:
                if field.changed:
                    obj = getattr(self._cfg, field.section_name, None)
                    if obj is not None:
                        try:
                            object.__setattr__(obj, field.field_name, field.value)
                        except Exception:
                            pass
        self._status = "[green]✓ Saved[/green]"

    def _focused_field(self) -> ConfigField | None:
        si, fi = self._cursor
        if fi < 0 or si >= len(self._sections):
            return None
        section = self._sections[si]
        if not section.expanded or fi >= len(section.fields):
            return None
        return section.fields[fi]

    def _move(self, delta: int) -> None:
        si, fi = self._cursor
        all_positions: list[tuple[int, int]] = []
        for s_idx, section in enumerate(self._sections):
            all_positions.append((s_idx, -1))
            if section.expanded:
                for f_idx in range(len(section.fields)):
                    all_positions.append((s_idx, f_idx))
        try:
            cur_pos = all_positions.index((si, fi))
            new_pos = max(0, min(len(all_positions) - 1, cur_pos + delta))
            self._cursor = all_positions[new_pos]
            # Scroll to keep cursor visible
            idx = new_pos
            if idx < self._scroll:
                self._scroll = idx
            elif idx >= self._scroll + self._MAX_VISIBLE:
                self._scroll = idx - self._MAX_VISIBLE + 1
        except ValueError:
            self._cursor = (0, -1)

    def _collapse(self) -> None:
        si, fi = self._cursor
        if fi >= 0:
            self._sections[si].expanded = False
            self._cursor = (si, -1)
        elif si > 0:
            self._sections[si].expanded = False


def _build_sections(cfg: Any) -> list[ConfigSection]:
    """Build ConfigSection list from AgenthiccConfig (or None)."""
    if cfg is None:
        return []
    SECTION_ATTRS = ["execution", "memory", "security", "api", "plugins"]
    sections: list[ConfigSection] = []
    for attr in SECTION_ATTRS:
        obj = getattr(cfg, attr, None)
        if obj is None:
            continue
        cls = type(obj)
        try:
            default = cls()
        except TypeError:
            default = obj
        fields: list[ConfigField] = []
        import dataclasses
        for f in dataclasses.fields(obj):
            val = getattr(obj, f.name)
            editable = f.type in (int, str, bool, float, "int", "str", "bool", "float")
            fields.append(ConfigField(
                section_name=attr,
                field_name=f.name,
                label=f.name,
                value=val,
                default=getattr(default, f.name, val),
                field_type=type(val),
                editable=editable,
            ))
        if fields:
            sections.append(ConfigSection(name=attr, fields=fields))
    return sections
```

### 3.5 Wiring /config Command

```python
# In CommandBus wiring (PRD-61 §3.4 extension):
bus.register(
    RunBuiltinCommand,
    lambda cmd: _handle_builtin(cmd, overlay_host, cfg),
)

def _handle_builtin(cmd: RunBuiltinCommand, overlay: OverlayHost, cfg: Any) -> None:
    if cmd.name == "config":
        overlay.show(ConfigMenuOverlay(
            cfg=cfg,
            on_close=overlay.hide,
        ))
```

---

## 4. Acceptance Criteria

| Criterion | Test |
|---|---|
| Shift+Tab cycles mode | `test_shift_tab_cycles_mode()` |
| Mode badge appears in footer | Snapshot test `footer_code_mode` |
| Mode notification shown 2s then clears | `test_mode_notification_clears()` |
| Mode passed to agent context | `test_mode_in_agent_context()` |
| Plugin can register custom mode | `test_plugin_registers_mode()` |
| `/config` opens ConfigMenuOverlay | `test_config_command_opens_overlay()` |
| Arrow keys navigate config fields | `test_config_nav_up_down()` |
| Enter on field enters edit state | `test_config_edit_field()` |
| "s" saves changed values to config | `test_config_save()` |
| Esc closes overlay | `test_config_esc_closes()` |
| Empty config shows helpful message | `test_config_empty_cfg()` |
