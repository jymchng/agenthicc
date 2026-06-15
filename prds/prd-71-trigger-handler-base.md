# PRD-71 — TriggerHandlerBase Mixin

## Problem

`TriggerHandler` is a `Protocol`. In Python, default method implementations defined
on a Protocol are methods of the Protocol class itself — they are **not inherited by
structural subtypes** that satisfy the Protocol without explicit `class Foo(TriggerHandler):`
inheritance. This means:

```python
class TriggerHandler(Protocol):
    def get_lines(self, item, width) -> list[str]:
        return [item.display[:width]]   # lives only on TriggerHandler

class AtMentionTrigger:                 # structural subtype — no inheritance
    char = "@"
    # get_lines is NOT here; Protocol default does NOT transfer
```

Calling `handler.get_lines(item, w)` on an `AtMentionTrigger` instance raises
`AttributeError` because the method does not exist on the handler class.

This is the root cause of the `@ AttributeError` bug introduced in PRD-69.
The same failure will repeat for every new optional Protocol method added in
the future if the structural-subtyping model is kept without mitigation.

---

## Goals

- Optional `TriggerHandler` methods with defaults (`get_lines`, `can_activate`,
  `get_hint`) are inherited by all in-tree handlers — adding a new optional method
  with a default never breaks existing handlers.
- External plugins may still satisfy `TriggerHandler` **structurally** (no
  explicit inheritance required) — open plugin extensibility is preserved.
- The type-checker continues to use `TriggerHandler` as the annotation type.
- Zero changes to callers (`trigger_picker.py`, `unified_session.py`,
  `tui_session.py`) — this is a pure data-model fix.

---

## Design: Protocol + concrete mixin

Two classes instead of one:

### `TriggerHandlerBase` (new, concrete mixin)

```python
class TriggerHandlerBase:
    """Concrete base class providing default implementations of optional
    TriggerHandler methods.

    In-tree handlers inherit this to gain working defaults. External plugins
    that satisfy TriggerHandler structurally need not inherit it.
    """
    char:  str = ""
    label: str = ""

    def can_activate(self, buf: list[str]) -> bool:
        return True

    def get_hint(self, item: MatchItem | None) -> str | None:
        return None

    def get_lines(self, item: MatchItem, available_width: int) -> list[str]:
        return [item.display[:available_width]]
```

### `TriggerHandler` (Protocol, unchanged interface)

The Protocol keeps its role as the **type-annotation surface**. Its method
signatures are unchanged. Default implementations are **removed** from the
Protocol body — the Protocol becomes a pure specification (signatures only),
which is the correct use of `Protocol` in Python's type system.

```python
@runtime_checkable
class TriggerHandler(Protocol):
    char:  str
    label: str

    def get_matches(self, fragment, ctx) -> list[MatchItem]: ...
    def on_select(self, item, fragment, buf) -> TriggerResult: ...
    def on_cancel(self, fragment, buf) -> list[str]: ...
    def can_activate(self, buf) -> bool: ...
    def get_hint(self, item) -> str | None: ...
    def get_lines(self, item, available_width) -> list[str]: ...
```

### In-tree handler inheritance

```python
class AtMentionTrigger(TriggerHandlerBase):   # inherits get_lines, get_hint
    char  = "@"
    label = "Mention File"
    # get_lines → inherited (single-column path display)
    # get_hint  → inherited (returns None)
    # can_activate → overridden for at-mention logic

class SlashCommandTrigger(TriggerHandlerBase):  # inherits get_hint, can_activate
    char  = "/"
    label = "Command"
    # get_lines → overridden for two-column wrapped layout
    # can_activate → overridden for slash-command logic
```

---

## What the split gives us

| Concern | TriggerHandler (Protocol) | TriggerHandlerBase (mixin) |
|---|---|---|
| Type annotations | ✓ — annotation type everywhere | — |
| Abstract contract enforcement | ✓ — type-checker verifies | — |
| External plugin extensibility | ✓ — structural subtyping | — |
| Default method inheritance | — | ✓ — concrete class |
| New optional method safety | — | ✓ — add to base, all in-tree handlers get it |

---

## File changes

| File | Change |
|---|---|
| `tui/trigger.py` | Add `TriggerHandlerBase`; remove default implementations from `TriggerHandler` Protocol body (pure signatures only); add `TriggerHandlerBase` to `__all__` |
| `tui/triggers/at_mention.py` | `class AtMentionTrigger(TriggerHandlerBase):`; remove now-redundant `get_hint` override (returns same as base) |
| `tui/triggers/slash_command.py` | `class SlashCommandTrigger(TriggerHandlerBase):`; `get_lines` override stays (custom two-column layout) |

No other files change.

---

## Acceptance criteria

- [ ] `AtMentionTrigger().get_lines(item, 80)` returns `[item.display[:80]]` without error.
- [ ] `SlashCommandTrigger(reg).get_lines(item, 80)` returns the two-column wrapped lines.
- [ ] `isinstance(AtMentionTrigger(), TriggerHandler)` is `True` (Protocol runtime check).
- [ ] `isinstance(SlashCommandTrigger(None), TriggerHandler)` is `True`.
- [ ] A class that satisfies `TriggerHandler` structurally (no inheritance) still passes `isinstance` check.
- [ ] All existing tests pass.
- [ ] `trigger_picker.py` calls `handler.get_lines()` without defensive `getattr` guards.
