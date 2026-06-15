# PRD-77 — Trigger Picker: _init_trigger Walk and Enter Submission

## Problems

### Bug 1 — Typing after a committed trigger token clears the input bar

**Steps to reproduce:**
1. Type `@do` — trigger picker opens
2. Select `@docs/` from the dropdown
3. Overlay closes; input bar shows `@docs/`
4. Type `.`
5. Press Enter

**Expected:** Input bar submits `@docs/.` as a mention.
**Actual:** Input bar is blank; the text is gone.

**Root cause trace:**

After step 2, `on_complete` sets session `_buf = ["@","d","o","c","s","/"]`.

At step 4, `InsertCapability.handle` runs with `ch = "."`:
1. `_find_trigger_tail()` walks backward through `["@","d","o","c","s","/"]`,
   skipping `"/"` (SlashCommandTrigger `can_activate` returns False because the
   preceding buffer is non-empty non-newline) and finding `"@"` at index 0
   with `AtMentionTrigger.can_activate([])` → True.
   Returns `("@", [], "docs/")`.
2. `session._buf.set([])` — **session buffer is cleared to pre-trigger content**.
3. `_open_trigger_overlay_with_initial(["@","d","o","c","s","/","."])`

Inside `_init_trigger` with `buf = ["@","d","o","c","s","/","."]`:
```python
last_char = buf[-1]          # "."
handler = registry.get(".")  # None — "." is not a trigger char
if handler is None:
    return                   # exits immediately; self._trigger stays None
```
The overlay opens with `self._trigger = None` and `self._matches = []`.

At step 5, `handle_key(ENTER)`:
```python
case Key.ENTER | Key.TAB:
    item = self._matches[self._selected] if self._matches else None  # None
    if self._trigger:   # False! _trigger is None
        ...
    else:
        self._complete(None)  # ← called with None
```
`on_complete(None)` pushes the session buffer which is still `[]`.
The input bar renders blank.

### Bug 2 — Typing a full mention with no matches requires two Enter presses

**Steps to reproduce:**
1. Type `@docs/.` completely (overlay remains open throughout)
2. Press Enter

**Expected:** Message is submitted.
**Actual:** Overlay closes, `@docs/.` appears in the input bar, but the message
is not submitted. A second Enter press is required.

**Root cause trace:**

While the overlay is open, `"docs/."` is accumulated in `_trigger.fragment`.
`get_matches("docs/.", ctx)` returns `[]` (hidden-file prefix; no matches).

On Enter:
```python
case Key.ENTER | Key.TAB:
    item = None  # matches empty
    if self._trigger:
        result = handler.on_select(None, "docs/.", [])
        # → TriggerResult(buffer=["@","d","o","c","s","/","."], submit=False)
        self._complete(result)  # closes overlay, sets buffer
```
`Enter` is consumed by the overlay. The buffer now holds `@docs/.` but the
message has not been dispatched. A second Enter through `_dispatch_idle` is
required.

---

## Root causes

### `_init_trigger` anchors on `last_char` — wrong when extra chars follow

`_init_trigger` opens with:
```python
last_char = buf[-1]
handler = registry.get(last_char)
if handler is None:
    return
```

This assumes the last character of `initial_buf` is the trigger char.  That
assumption holds when `_open_trigger_overlay` appends the trigger char:
`initial = buf + [trigger_char]`.

It breaks when `InsertCapability` appends an extra char:
`initial = tpre + [tch] + list(tfrag) + [ch]`
where `ch` is the new character typed (`.`).  The last char is `.`, which has
no handler, so `_init_trigger` bails immediately without finding `@`.

The loop that follows `last_char` also has a secondary bug: it finds any
trigger char in the buffer but applies `last_char`'s handler to it.  If
`last_char` is `"/"` and the loop finds `"@"`, it sets
`self._trigger.handler = SlashCommandTrigger` instead of `AtMentionTrigger`.

### `ENTER` and `TAB` share one case; Enter never signals submission

`case Key.ENTER | Key.TAB` always reconstructs the buffer and closes the
overlay.  Whether the user pressed Tab (intending to commit a completion and
keep typing) or Enter (intending to submit the composed text) is never
distinguished.  When there are no matches and Enter is pressed, the overlay
closes but `submit=False` — the Enter is consumed and the user must press Enter
again.

---

## Goals

- Typing any character after a committed trigger token (e.g. `@docs/` + `.`)
  re-opens the trigger overlay correctly, with the new character included in
  the fragment.
- `_init_trigger` correctly identifies the trigger char and its handler
  regardless of what the last character of `initial_buf` is.
- Enter with no dropdown match commits the text AND submits the message
  (single Enter press).
- Tab still only commits without submitting (user continues typing).

---

## Fix 1 — Rewrite `_init_trigger` to walk backward for the trigger char

Remove the `last_char` anchor.  Walk backward through the buffer to find the
rightmost activatable trigger char and use **that char's own handler**.

```python
def _init_trigger(self) -> None:
    buf = self._buf.buf
    if not buf:
        return
    # Walk backward to find the rightmost activatable trigger char.
    # Do NOT anchor on last_char — it may be a regular char appended after
    # the trigger (e.g. "@docs/" + "." from InsertCapability re-entry).
    for i in range(len(buf) - 1, -1, -1):
        ch = buf[i]
        if ch.isspace():
            break   # stop at a word boundary; no trigger can activate here
        if ch in (self._registry.chars if self._registry else set()):
            handler  = self._registry.get(ch)   # handler for THIS char
            pre      = buf[:i]
            fragment = "".join(buf[i + 1:])
            if handler and handler.can_activate(pre):
                self._trigger = SimpleNamespace(
                    handler=handler, char=ch,
                    fragment=fragment, pre_buf=list(pre),
                )
                self._buf.set(list(pre))
                self._update_matches()
                break
```

This fixes both the missing-trigger case (`"."` last char, `"@"` is earlier)
and the wrong-handler case (previously `last_char`'s handler was used for
whatever trigger char the loop found).

---

## Fix 2 — Separate `ENTER` and `TAB` in `handle_key`

Split `case Key.ENTER | Key.TAB` into two cases:

**Enter** — when there are no matches, the user has confirmed the raw text they
typed and wants to submit it immediately.  Set `submit=True` on the result so
`on_complete` dispatches `SendMessageCommand` without requiring a second Enter.
When there IS a match selected, Enter commits the selection (same as Tab).

**Tab** — always commits the selection without submitting; the user continues
typing after the inserted text.

```python
case Key.ENTER:
    item = self._matches[self._selected] if self._matches else None
    if self._trigger:
        result = self._trigger.handler.on_select(
            item, self._trigger.fragment, self._buf.buf
        )
        if item is None:
            # No match — commit text AND submit so the user does not
            # need a second Enter press.
            result = TriggerResult(
                buffer=result.buffer, submit=True, cursor=result.cursor
            )
        self._complete(result)
    else:
        self._complete(None)

case Key.TAB:
    item = self._matches[self._selected] if self._matches else None
    if self._trigger:
        result = self._trigger.handler.on_select(
            item, self._trigger.fragment, self._buf.buf
        )
        self._complete(result)   # commit only; user continues typing
    else:
        self._complete(None)
```

---

## File changes

| File | Change |
|---|---|
| `tui/workspace/overlays/trigger_picker.py` | Rewrite `_init_trigger` (Fix 1); split `ENTER | TAB` case (Fix 2) |

No other files change.

---

## Acceptance criteria

- [ ] `@do` → select `@docs/` → type `.` → press Enter: message `@docs/.` is submitted without the input bar clearing.
- [ ] `@docs/.` typed in full → press Enter once: message is submitted.
- [ ] `@do` → select `@docs/` → Tab: selection is committed, overlay closes, input bar shows `@docs/ `, user can continue typing. No submission.
- [ ] `@` → type fragment → enter text from dropdown matches → Enter submits.
- [ ] Trigger char and handler are always consistent (`@` → `AtMentionTrigger`, `/` → `SlashCommandTrigger`).
- [ ] All existing tests pass.
