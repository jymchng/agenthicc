# PRD-69 — Unified Trigger Manager

## Problem

The trigger system (`@`, `/`) is well-structured at its core but has three
specific leaks that make adding new trigger characters (`!`, `#`) require
touching multiple unrelated files:

1. **`Key.AT` is hardcoded in two files.** `unified_session.py`
   (`_is_trigger_char`) and `trigger_picker.py` (`handle_key`) both contain
   separate special-cases for the `@` key. Every new trigger character with a
   non-standard `Key.*` constant requires the same duplication.

2. **`on_select` returns `list[str]` (raw buffer), not a typed result.**
   Every trigger collapses to "update buffer". There is no way for a handler to
   signal "submit immediately" (needed for `!` bash) or "position cursor
   inside" (needed for `#agent` mention).

3. **`TriggerPickerOverlay` is coupled to the full `TriggerRegistry`.**
   The overlay does its own handler lookup via `_init_trigger()` rather than
   receiving a pre-resolved handler. This means the overlay has to know about
   the registry's internal structure and character normalisation.

---

## Goals

- Adding a new trigger character requires creating one new `TriggerHandler`
  class and one `manager.register(...)` call — nothing else.
- `Key.*` normalisation lives in exactly one place.
- Handlers can express post-selection behaviour (insert only, insert+submit,
  cursor placement) without changes to any calling code.
- The overlay is a pure rendering surface; it receives a resolved handler and
  emits a `TriggerResult`.

---

## Non-goals

- Do not implement `!` (BashTrigger) or `#` (AgentMentionTrigger) — those are
  follow-on PRDs. This PRD only lays the infrastructure.
- Do not change the dropdown rendering (`MatchItem`, `TriggerPickerOverlay`
  layout, scrolling, hint display).
- Do not change `_find_trigger_tail` / backspace-reopen mechanics.
- Do not add async to any `TriggerHandler` method.

---

## Data model changes

### `TriggerResult` (new dataclass)

```python
@dataclass
class TriggerResult:
    buffer: list[str]         # new buffer content after selection
    submit: bool = False      # if True, dispatch SendMessageCommand immediately
    cursor: int | None = None # explicit cursor position; None = end of buffer
```

Replaces the bare `list[str]` return type of `on_select`. Every completion
path now carries intent, not just buffer bytes.

### `MatchItem` — unchanged

### `TriggerContext` — minimal change

Remove the unused `history` field. Add two optional fields for handlers that
need runtime context:

```python
@dataclass
class TriggerContext:
    cwd: Path
    session_id: str = ""          # scope results to a session if needed
    command_registry: Any = None  # CommandRegistry, for cross-trigger lookups
```

### `TriggerHandler` Protocol — one addition, one changed return type

```python
class TriggerHandler(Protocol):
    char: str    # single activation character — unchanged
    label: str   # NEW: human-readable name ("Mention File", "Command", "Shell", "Agent")

    def can_activate(self, buf: list[str]) -> bool: ...              # unchanged
    def get_matches(self, fragment: str, ctx: TriggerContext) -> list[MatchItem]: ...  # unchanged
    def on_select(self, item: MatchItem | None, fragment: str, buf: list[str]) -> TriggerResult: ...  # was list[str]
    def on_cancel(self, fragment: str, buf: list[str]) -> list[str]: ...  # unchanged
    def get_hint(self, item: MatchItem | None) -> str | None: ...    # unchanged
```

The only breaking change: `on_select` return type changes from `list[str]` to
`TriggerResult`. Existing handlers each need a one-line update.

---

## `TriggerManager` (replaces `TriggerRegistry`)

```python
class TriggerManager:
    """Single source of truth for all trigger characters and their handlers."""

    def register(self, handler: TriggerHandler) -> None: ...
    def unregister(self, char: str) -> None: ...
    def get(self, char: str) -> TriggerHandler | None: ...

    @property
    def chars(self) -> frozenset[str]: ...

    def resolve(self, key: Key, ch: str) -> str | None:
        """Map a (Key, ch) pair to a registered trigger char, or None.

        This is the single place where key-enum normalisation lives.
        Key.AT → "@" if "@" is registered.
        Any other Key.CHAR ch that is registered maps to itself.
        No other file ever inspects Key enums for trigger detection.
        """
```

`resolve()` is the structural fix. `unified_session.py` calls
`self._triggers.resolve(key, ch)` and receives a `str | None` — no key-enum
knowledge required. The overlay never sees `Key.*` for trigger-char purposes.

---

## File-by-file changes

| File | Change |
|---|---|
| `tui/trigger.py` | Add `TriggerResult`; remove `history` from `TriggerContext`; add `session_id` and `command_registry`; add `label` to `TriggerHandler` Protocol; rename `TriggerRegistry` → `TriggerManager`; add `resolve()` method |
| `tui/triggers/at_mention.py` | `on_select` returns `TriggerResult(buffer=...)` |
| `tui/triggers/slash_command.py` | `on_select` returns `TriggerResult(buffer=...)` |
| `tui/input/unified_session.py` | `_is_trigger_char` replaced by `self._triggers.resolve(key, ch)`; `on_complete` callback handles `submit=True` and explicit `cursor` from `TriggerResult` |
| `tui/workspace/overlays/trigger_picker.py` | Remove `Key.AT` special-case in `handle_key`; pass resolved `TriggerResult` through `on_complete`; receive pre-resolved handler instead of full registry |
| `runners/tui_session.py` | `TriggerRegistry()` → `TriggerManager()` |

No new files required. No changes outside the trigger subsystem.

---

## Example: adding `!` (BashTrigger) after this PRD

```python
class BashTrigger:
    char = "!"
    label = "Shell"

    def can_activate(self, buf): return not buf or buf[-1] == "\n"
    def get_matches(self, fragment, ctx): return [...]  # shell history / common cmds
    def on_select(self, item, fragment, buf):
        cmd = item.value if item else fragment
        return TriggerResult(buffer=buf + list("!" + cmd), submit=True)
    def on_cancel(self, fragment, buf): return buf + list("!" + fragment)
    def get_hint(self, item): return item.hint if item else None
```

Registration:

```python
manager.register(BashTrigger())
```

No other file changes. `submit=True` in `TriggerResult` causes
`unified_session` to dispatch `SendMessageCommand` immediately after updating
the buffer.

## Example: adding `#` (AgentMentionTrigger) after this PRD

```python
class AgentMentionTrigger:
    char = "#"
    label = "Agent"

    def get_matches(self, fragment, ctx):
        return [MatchItem(display=a.name, value=a.id) for a in agents if ...]

    def on_select(self, item, fragment, buf):
        prefix = list(f"#{item.value} ")
        return TriggerResult(buffer=buf + prefix, cursor=len(buf) + len(prefix))
```

---

## Acceptance criteria

- [ ] `TriggerManager.resolve(key, ch)` is the only place in the codebase that
  maps `Key.AT` → `"@"` or any other key-enum to a trigger char string.
- [ ] `unified_session.py` contains no reference to `Key.AT` for trigger
  detection.
- [ ] `trigger_picker.py` contains no `Key.AT` special-case in `handle_key`.
- [ ] Adding a new trigger requires: one new class, one `manager.register()`
  call, no other file changes.
- [ ] `TriggerResult.submit=True` causes `unified_session` to dispatch
  `SendMessageCommand` after buffer update.
- [ ] All existing tests pass. `/skills`, `@mention`, and `/command` triggers
  work identically to before.
