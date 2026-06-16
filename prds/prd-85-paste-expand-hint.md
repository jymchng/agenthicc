# PRD-85 — Paste-Condensed Expansion Hint

## Background

When a large paste is applied, `PasteState.apply()` condenses it to a
single label line in the composer:

```
❯ [Pasted text #3 286 chars]▌
```

Nothing on screen tells the user how to expand the paste or delete it.
The footer row 2 shows the generic idle hints (`Enter Submit │ Ctrl+J
Newline │ /cmd │ @Mention`) which make no mention of Ctrl+V.

## Goals

- While a paste is condensed, footer row 2 shows paste-specific action
  hints instead of the generic idle hints.
- The hint disappears automatically the moment Ctrl+V is pressed (paste
  expands, `paste_condensed` signal → False → footer re-renders).
- No new signals, no new state, no changes outside the footer component.

## Non-Goals

- Changing how paste condensation / expansion works.
- Showing the hint in the status bar or as a transient notification.

---

## Design

### Priority stack for footer row 2

```
1. conv.notification()         — transient (mode switch, Ctrl+C warning)
2. inp.paste_condensed()       — NEW: paste-specific action hints
3. normal state-based hints    — from _HINTS dict keyed on AgentState
```

### Hint string

```
Ctrl+V Expand paste  Backspace Delete  Enter Submit as-is
```

Formatted by `_build_hints` (bold key, dim description, `│` separators,
width-truncated to terminal cols).

### Implementation

One new `elif` branch in `FooterComponent.render()`:

```python
notif = conv.notification()
if notif:
    hints_str = _fit(f"[dim]{notif}[/dim]", cols)
elif self._state.input.paste_condensed():
    hints_str = _build_hints(
        "Ctrl+V Expand paste  Backspace Delete  Enter Submit as-is", cols
    )
else:
    state_name = conv.agent_state().name.lower()
    raw_hints  = _HINTS.get(state_name, _HINTS["idle"])
    hints_str  = _build_hints(raw_hints, cols)
```

`inp.paste_condensed` is already subscribed to `_redraw` in
`Workspace.start()`, so the footer re-renders automatically when the
signal changes — no wiring changes needed.

---

## Illustration

**Paste condensed:**
```
❯ [Pasted text #1 286 chars]▌
────────────────────────────────────────────────────────────────────────────────
  AUTO  (shift+tab to cycle)  │  ctrl+j = ↵
Ctrl+V Expand paste  │  Backspace Delete  │  Enter Submit as-is
```

**After Ctrl+V (paste expanded):**
```
❯ def greet(name):
      print(f"Hello, {name}")
      return True▌
────────────────────────────────────────────────────────────────────────────────
  AUTO  (shift+tab to cycle)  │  ctrl+j = ↵
Enter Submit  │  Ctrl+J Newline  │  /cmd  │  @Mention
```

---

## File changes

| File | Change |
|---|---|
| `tui/workspace/components.py` | Add `elif inp.paste_condensed():` branch in `FooterComponent.render()` |
| `prds/prd-68-feature-expectations.md` | Update §3.10 and add §4.5 |

---

## Acceptance criteria

- [ ] When a paste is condensed, footer row 2 shows
      `Ctrl+V Expand paste │ Backspace Delete │ Enter Submit as-is`.
- [ ] The moment Ctrl+V is pressed the paste-specific hint disappears and
      normal hints return.
- [ ] The moment Backspace deletes the paste the hint disappears and
      normal hints return.
- [ ] A transient `conv.notification()` (e.g. mode-switch message) still
      takes priority over the paste hint.
- [ ] All existing tests pass.
