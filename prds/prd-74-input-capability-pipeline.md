# PRD-74 — Input Capability Pipeline

## Problem

### Immediate bug

`@` does not work during streaming input. When `@` is pressed it arrives as
`Key.AT`. `_dispatch_streaming` only detects triggers inside
`case Key.CHAR if ch:`, which `Key.AT` never matches, so the key is silently
dropped. `_dispatch_idle` correctly calls `resolve(key, ch)` before the match
statement, which normalises `Key.AT → "@"`. The fix cannot simply be "copy the
idle trigger block into streaming" because that creates a third maintenance
surface.

### Structural problem

`_dispatch_idle` and `_dispatch_streaming` are monolithic functions that
duplicate large portions of logic:

| Feature | idle | streaming |
|---|---|---|
| Bracketed paste / Ctrl+V | ✓ | ✓ |
| Ctrl+J newline | ✓ | ✓ |
| Backspace / Ctrl+U | ✓ | ✓ |
| Insert regular char | ✓ | ✓ |
| Trigger detection (`@`, `/`) | ✓ | ✓ (but broken for `Key.AT`) |
| History navigation | ✓ | — |
| Cursor movement | ✓ | — |
| Submit immediately | ✓ | — |
| Queue and confirm | — | ✓ |
| Interrupt agent | — | ✓ |
| Mode cycling | ✓ | — |

Every new feature (a new trigger char, a new key binding, a new mode) must be
added to each function independently. Every new mode requires deciding which
fragments to copy. This is the root cause of the `@` bug and will cause the
same class of bug for future triggers (`#`, `!`) and future modes.

---

## Goals

- `@` (and all trigger chars) work identically in every input mode.
- Adding a new trigger char requires one `manager.register()` call — nothing
  else.
- Adding a new input mode requires declaring a capability list — no changes to
  existing code.
- `_dispatch_idle` and `_dispatch_streaming` are deleted; the dispatcher is a
  single ordered pipeline.
- No changes to `TriggerManager`, `InputBuffer`, `PasteState`, `CommandBus`,
  `Workspace`, or any overlay.

---

## Design

### Capability

A **capability** is a single-responsibility, async key handler:

```python
class Capability(Protocol):
    async def handle(self, key: Key, ch: str, session: InputSession) -> bool:
        """Handle the keystroke. Return True if consumed, False to pass through."""
        ...
```

Each capability holds its own configuration, accesses the session through a
thin interface, and knows nothing about other capabilities.

### InputSession interface (thin facade over UnifiedInputSession)

Capabilities receive a `session` reference that exposes only what they need:

```python
@dataclass
class InputSession:
    buf:                list[str]                    # current buffer
    cursor:             int                          # current cursor position
    paste:              PasteState
    hist:               HistoryNavigator
    registry:           TriggerManager | None
    overlay:            OverlayHost | None
    modes:              ModeManager
    ctrl_c_count:       int
    cfg:                Any                          # AgenthiccConfig
    conversation:       ConversationStore

    # Mutators (thin wrappers over UnifiedInputSession private methods)
    push:               Callable[[], None]           # _push()
    submit:             Callable[[str], Awaitable]   # _submit()
    open_overlay:       Callable[[str], Awaitable]   # _open_trigger_overlay()
    dispatch:           Callable[[Any], Awaitable]   # command_bus.dispatch_async()
    exit:               Callable[[], None]           # signals _EXIT
```

Capabilities do not import `UnifiedInputSession` directly and do not call
private methods on it.

### Capability pipeline

The dispatcher tries capabilities in order until one returns `True`:

```python
async def _dispatch(self, key: Key, ch: str) -> object:
    session = self._make_session()
    for cap in self._capabilities:
        if await cap.handle(key, ch, session):
            return None
    return None
```

`self._capabilities` is set when `set_mode()` is called and comes from a
mode-to-capability-list registry.

### Capabilities (in-tree)

Each of these replaces one or more arms of the old match statements.

| Class | Replaces | Config |
|---|---|---|
| `OverlayCapability` | overlay routing in main loop | — |
| `TriggerCapability` | trigger detection in both dispatchers | — |
| `PasteCapability` | `Key.PASTE` + `Key.CTRL_V` | — |
| `SubmitCapability` | `Key.ENTER` | `queue: bool` |
| `NewlineCapability` | `Key.CTRL_ENTER` | — |
| `InterruptCapability` | `Key.CTRL_C` + `Key.ESC` in streaming | — |
| `CtrlCCapability` | double-Ctrl+C sequence in idle | — |
| `ClearCapability` | `Key.CTRL_U` | — |
| `BackspaceCapability` | `Key.BACKSPACE` including trigger re-enter | — |
| `CursorCapability` | `Key.LEFT/RIGHT/HOME/END` | — |
| `HistoryCapability` | `Key.UP/DOWN` at line boundaries | — |
| `ModeCycleCapability` | `Key.SHIFT_TAB` | — |
| `InsertCapability` | regular char insert (always last) | — |

### Mode declarations

```python
IDLE_CAPABILITIES: list[Capability] = [
    OverlayCapability(),
    TriggerCapability(),          # @, /, #, ! via TriggerManager
    PasteCapability(),
    CtrlCCapability(),
    CtrlDCapability(),
    SubmitCapability(queue=False),
    NewlineCapability(),
    BackspaceCapability(),
    ClearCapability(),
    CursorCapability(),
    HistoryCapability(),
    ModeCycleCapability(),
    InsertCapability(),           # fallback — must be last
]

STREAMING_CAPABILITIES: list[Capability] = [
    OverlayCapability(),
    TriggerCapability(),          # identical — @ and / work exactly as in idle
    PasteCapability(),
    SubmitCapability(queue=True), # Enter → SendMessageCommand (may queue)
    NewlineCapability(),
    InterruptCapability(),        # Ctrl+C, ESC → InterruptAgentCommand
    BackspaceCapability(),
    ClearCapability(),
    InsertCapability(),           # fallback — must be last
]
```

`InsertCapability` always returns `True`, so it acts as the catch-all and must
appear last.

---

## Adding a new trigger char — zero changes required

```python
manager.register(AgentMentionTrigger())   # one line
```

`TriggerCapability` calls `manager.resolve()` on every keystroke. Both
`IDLE_CAPABILITIES` and `STREAMING_CAPABILITIES` include `TriggerCapability`.
Both modes automatically support `#` with zero further changes.

---

## Adding a new mode — one declaration required

```python
REVIEWING_CAPABILITIES: list[Capability] = [
    OverlayCapability(),
    TriggerCapability(),          # still want @/@-mention while reviewing
    PasteCapability(),
    SubmitCapability(queue=False),
    CtrlCCapability(),
    BackspaceCapability(),
    ClearCapability(),
    InsertCapability(),
    # HistoryCapability omitted — no history in review mode
    # ModeCycleCapability omitted — cannot switch modes while reviewing
    # CursorCapability omitted — no cursor navigation in review mode
]
```

No changes to any existing capability, any existing mode, or `UnifiedInputSession`.

---

## File changes

| File | Change |
|---|---|
| `tui/input/capabilities.py` | **New** — one class per capability (~200 lines total) |
| `tui/input/unified_session.py` | Delete `_dispatch_idle`, `_dispatch_streaming`; add `_dispatch()`, `_make_session()`, `set_mode()` updates capability list |
| `tui/input/__init__.py` | Export `Capability`, `InputSession` |

No other files change.

---

## `TriggerCapability` in detail

This is the capability that fixes the immediate bug and must handle the
`Key.AT` / `Key.CHAR` normalisation correctly:

```python
class TriggerCapability:
    """Handles any registered trigger char regardless of input mode.

    Uses TriggerManager.resolve() which normalises Key.AT → "@" in one place.
    Works identically in IDLE and STREAMING — this is why @ works everywhere.
    """

    async def handle(self, key: Key, ch: str, session: InputSession) -> bool:
        tch = session.registry.resolve(key, ch) if session.registry else None
        if tch is None:
            return False
        handler = session.registry.get(tch)
        can_open = (
            handler.can_activate(session.buf[:session.cursor])
            if handler else False
        )
        if can_open:
            await session.open_overlay(tch)
        else:
            # Trigger char typed in a context where it can't activate
            # (e.g. "@" mid-word) — insert literally.
            session.buf.append(tch)        # simplified; real impl uses InputBuffer
            session.push()
        return True
```

---

## Migration notes

- The old `_dispatch_streaming` and `_dispatch_idle` must be deleted entirely
  (no backwards-compat shims per memory feedback_no_backwards_compat).
- The `InputMode` enum stays — it drives `set_mode()` which swaps the
  capability list.
- `UnifiedInputSession.__init__` still takes the same parameters; the
  capability lists are module-level constants, not constructor arguments.
- Capabilities that need async (trigger, submit, interrupt) are `async def`.
  Capabilities that don't (insert, cursor) may be `async def` anyway for a
  uniform interface (they just return immediately).

---

## Acceptance criteria

- [ ] `@` opens the trigger picker in streaming mode.
- [ ] `@` opens the trigger picker in idle mode (unchanged).
- [ ] `/` opens the trigger picker in both modes (unchanged).
- [ ] `_dispatch_idle` and `_dispatch_streaming` no longer exist.
- [ ] Adding a new trigger char to `TriggerManager` requires no changes to
      `unified_session.py` or any capability.
- [ ] `STREAMING_CAPABILITIES` and `IDLE_CAPABILITIES` are the single source
      of truth for what each mode supports.
- [ ] All existing tests pass.
