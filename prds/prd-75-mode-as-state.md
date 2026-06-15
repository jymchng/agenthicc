# PRD-75 — Mode as First-Class AppState Signal

## Problems

### 1. Shift+Tab does nothing (immediate bug)

`build_default_registry()` in `tui/runtime/mode_manager.py` wraps the import
of `agenthicc.modes` in a bare `except Exception: pass`.  When the import
succeeds it then accesses `existing_mm._registry` — a private attribute — via
`hasattr`.  If `_registry` is not the right attribute name, `hasattr` returns
False, the loop never runs, and the registry contains only "Auto".

With one mode in the registry, `ModeManager.cycle()` is a no-op:

```python
def cycle(self):
    modes = self._registry.all()
    if len(modes) > 1:        # guard never passes
        self._idx = ...
    return self.active         # same mode every time
```

Shift+Tab fires, writes the same three signals back, and the footer is
unchanged.  The bug is invisible because the error is swallowed.

### 2. Mode state is scattered across three signals in the wrong store

`ConversationStore` holds `active_mode_name`, `active_mode_badge`, and
`mode_str` — three signals for one logical concept.  Every caller that changes
the mode must write all three manually and keep them in sync.

These signals belong in `AppState`, not `ConversationStore`.
`ConversationStore` is for conversation / transcript state; mode is a session
runtime concept.

### 3. ModeManager is not reactive

`ModeManager` holds the authoritative `_idx` (or `_active`) but has no
signals.  After `cycle()` is called, the caller must manually propagate the
result into the three `ConversationStore` signals.  This is a latent bug
vector: any future caller that only writes two of the three signals leaves the
store inconsistent.

---

## Goals

- Shift+Tab cycles through all registered modes (Auto → Plan → Ask → Review →
  Safe → Debug → Auto).
- Mode is represented as a single `Signal[RuntimeMode]` on `AppState` — one
  write, automatic propagation.
- `ModeManager.cycle()` and `set_by_name()` write the signal internally;
  callers need no knowledge of signals.
- `build_default_registry()` no longer swallows errors silently.
- `active_mode_name`, `active_mode_badge`, and `mode_str` are removed from
  `ConversationStore`.

---

## Design

### `AppState` gains one signal

```python
class AppState:
    conversation: ConversationStore
    input:        InputState
    active_mode:  Signal[RuntimeMode]   # NEW — replaces three ConversationStore signals
    overlay:      Signal[str]
    modal_open:   Signal[bool]
```

`RuntimeMode` is already `@dataclass(frozen=True)`.  No change to its fields.

### `ModeManager` takes `AppState` and owns the write

```python
class ModeManager:
    def __init__(self, registry: ModeRegistry, app_state: AppState) -> None:
        self._registry = registry
        self._app_state = app_state

    def cycle(self) -> RuntimeMode:
        modes = self._registry.all()
        if len(modes) > 1:
            # advance
        new_mode = self.active
        self._app_state.active_mode.set(new_mode)   # ONE write
        return new_mode

    def set_by_name(self, name: str) -> RuntimeMode | None:
        ...
        self._app_state.active_mode.set(mode)
        return mode
```

### `build_default_registry()` is fixed

Use the public API (`all_modes()`) instead of the private `._registry`
attribute, and log failures instead of swallowing them:

```python
def build_default_registry() -> ModeRegistry:
    reg = ModeRegistry()
    try:
        from agenthicc.modes.builtin import build_default_registry as _bdr
        from agenthicc.modes.manager import ModeManager as _ExistingMM
        existing_mm = _ExistingMM(_bdr())
        for mode in existing_mm._registry.all_modes():
            reg.register(RuntimeMode(
                name=mode.name,
                badge=mode.label,
                description=mode.description,
                system_prompt_suffix=mode.system_patch,
            ))
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Could not load modes: %s", exc)
    if not reg.get("Auto"):
        reg.register(RuntimeMode(name="Auto", badge="⏵⏵", description="Automatic"))
    return reg
```

### `ModeCycleCapability` shrinks to one write

```python
async def handle(self, key, ch, session):
    if key != Key.SHIFT_TAB:
        return _PASS
    new_mode = session._modes.cycle()   # writes signal internally
    session._state.conversation.notification.set(f"❖ Switched to {new_mode.name} mode")
    asyncio.get_running_loop().call_later(
        2.0, lambda: session._state.conversation.notification.set(None)
    )
    return _CONSUMED
```

### `FooterComponent` reads from `app_state.active_mode`

```python
def render(self):
    mode = self._state.active_mode()
    mode_line = f"{mode.badge} {mode.name}  (shift+tab to cycle)  │  ctrl+j = ↵"
    ...
```

`build_mode_str()` helper is no longer needed.

---

## File changes

| File | Change |
|---|---|
| `tui/conversation_store.py` | Remove `active_mode_name`, `active_mode_badge`, `mode_str` signals from `ConversationStore`; add `active_mode: Signal[RuntimeMode]` to `AppState` |
| `tui/runtime/mode_manager.py` | `ModeManager.__init__` takes `app_state`; `cycle()` and `set_by_name()` write signal; fix `build_default_registry()` |
| `tui/workspace/components.py` | `FooterComponent` reads `self._state.active_mode()` |
| `tui/workspace/workspace.py` | Subscribe `self._state.active_mode` to `_redraw`; remove old subscriptions |
| `tui/input/capabilities.py` | `ModeCycleCapability` — remove three manual signal writes |
| `runners/tui_session.py` | Pass `app_state` to `ModeManager()` |

---

## Acceptance criteria

- [ ] Shift+Tab cycles through all 6 built-in modes (Auto → Plan → Ask →
      Review → Safe → Debug → Auto).
- [ ] Footer badge and mode name update immediately on each Shift+Tab press.
- [ ] `ConversationStore` no longer has `active_mode_name`, `active_mode_badge`,
      or `mode_str` fields.
- [ ] `AppState.active_mode` is a `Signal[RuntimeMode]` initialised to the
      "Auto" mode.
- [ ] `ModeManager.cycle()` and `set_by_name()` write `app_state.active_mode`
      internally — callers do not write it.
- [ ] `build_default_registry()` logs a warning instead of swallowing errors.
- [ ] All existing tests pass.
