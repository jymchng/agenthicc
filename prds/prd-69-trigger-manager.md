# PRD-69 — Unified Trigger Manager

## Problem

The trigger system (`@`, `/`) is well-structured at its core but has four
specific issues that make adding new trigger characters (`!`, `#`) error-prone
and prevent full description display in the dropdown:

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
   receiving a pre-resolved handler.

4. **Dropdown descriptions are truncated and cannot wrap.**
   `SlashCommandTrigger.get_matches` pre-truncates descriptions to 36 chars.
   `TriggerPickerOverlay` further clips each item to 60 chars and renders it
   as exactly one terminal line per item. Long descriptions (e.g.
   "List all registered commands with their source and group") are always
   silently cut off with no way to see the full text.

---

## Goals

- Adding a new trigger character requires creating one new `TriggerHandler`
  class and one `manager.register(...)` call — nothing else.
- `Key.*` normalisation lives in exactly one place.
- Handlers can express post-selection behaviour (insert only, insert+submit,
  cursor placement) without changes to any calling code.
- The overlay is a pure rendering surface; it receives a resolved handler and
  emits a `TriggerResult`.
- Long descriptions display fully, wrapped to the next line under the same
  column as the description text, without overflowing the overlay height.

---

## Non-goals

- Do not implement `!` (BashTrigger) or `#` (AgentMentionTrigger) — those are
  follow-on PRDs. This PRD only lays the infrastructure.
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

### `MatchItem` — structured fields added

```python
@dataclass
class MatchItem:
    display: str          # computed single-line fallback (backwards compat)
    value:   str          # text inserted into buffer on selection — unchanged
    hint:    str = ""     # below-dropdown annotation — unchanged
    label:   str = ""     # left column (e.g. "/commands", "@docs/index.md")
    detail:  str = ""     # right column — full, untruncated description/path
```

`label` and `detail` carry the raw, untruncated data. When both are set,
`TriggerPickerOverlay` uses them (via `handler.get_lines`) instead of
`display`. When absent, `display` is used as before — fully backwards
compatible.

`get_matches` implementations stop pre-truncating: the full description goes
into `detail`, and `display` is kept as a short fallback for any consumer that
doesn't know about `label`/`detail`.

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

### `TriggerHandler` Protocol — two additions, one changed return type

```python
class TriggerHandler(Protocol):
    char:  str   # single activation character — unchanged
    label: str   # NEW: human-readable name ("Mention File", "Command", "Shell", "Agent")

    def can_activate(self, buf: list[str]) -> bool: ...               # unchanged
    def get_matches(self, fragment: str, ctx: TriggerContext) -> list[MatchItem]: ...  # unchanged
    def on_select(self, item: MatchItem | None, fragment: str, buf: list[str]) -> TriggerResult: ...  # was list[str]
    def on_cancel(self, fragment: str, buf: list[str]) -> list[str]: ...  # unchanged
    def get_hint(self, item: MatchItem | None) -> str | None: ...     # unchanged

    def get_lines(self, item: MatchItem, available_width: int) -> list[str]:
        """Return the terminal lines to display for one dropdown item.

        Default implementation returns a single line using item.display,
        clipped to available_width. Handlers override this to implement
        two-column layout with description wrapping.

        The overlay calls this once per visible item per render. The returned
        list length determines how many terminal rows that item occupies.
        """
        return [item.display[:available_width]]
```

`get_lines` is the key addition for multi-line display. It is an **optional
override** — the default returns one line, so existing handlers that do not
override it continue to work without change.

`SlashCommandTrigger` overrides `get_lines` to produce:

```
  ▶ /commands              List all registered commands with
                           their source and group
```

The second line is indented to align under the description column. The handler
computes this layout given `available_width` at render time.

The only **breaking** change is `on_select` return type (`TriggerResult`
instead of `list[str]`). Each existing handler needs a one-line update.

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

## `TriggerPickerOverlay` — line-height-aware scroll model

The current scroll model assumes **one item = one terminal line**:

```python
# current (item-count-based)
n      = min(_MAX_VISIBLE, len(matches))
scroll = max(0, min(selected - n + 1, len(matches) - n))
for i, item in enumerate(matches[scroll:scroll + n]):
    lines.append(Text(item.display[:60]))   # exactly 1 line per item
```

With `get_lines` returning variable-height items, the model must track
**terminal lines**, not items:

```python
# new (line-count-based)
available_width = shutil.get_terminal_size((80, 24)).columns - 4  # indent
item_lines  = [handler.get_lines(item, available_width) for item in matches]
item_heights = [len(ls) for ls in item_lines]   # lines per item

# cumulative line offsets — map item index → first terminal line
offsets = [0]
for h in item_heights:
    offsets.append(offsets[-1] + h)

# scroll tracks the item index whose first line is at the top of the window
# ensure the selected item's last line is within the visible window
```

`_MAX_VISIBLE` becomes a terminal-line budget (default 12), not an item count.
The scroll computation uses cumulative offsets to keep the selected item fully
visible — the selected item's first **and** last lines are inside the window.

The overlay stores `_scroll_item: int` (index of the topmost visible item).
On Up/Down navigation, after updating `_selected`, the overlay adjusts
`_scroll_item` so the selected item is always fully in view.

---

## File-by-file changes

| File | Change |
|---|---|
| `tui/trigger.py` | Add `TriggerResult`; add `label`/`detail` to `MatchItem`; remove `history` from `TriggerContext`; add `session_id` and `command_registry`; add `label` and `get_lines` to `TriggerHandler` Protocol; rename `TriggerRegistry` → `TriggerManager`; add `resolve()` |
| `tui/triggers/at_mention.py` | `on_select` returns `TriggerResult(buffer=...)`; populate `label`/`detail` in returned `MatchItem`s |
| `tui/triggers/slash_command.py` | `on_select` returns `TriggerResult(buffer=...)`; stop pre-truncating descriptions; populate `label`/`detail`; override `get_lines` for two-column wrapped layout |
| `tui/input/unified_session.py` | `_is_trigger_char` replaced by `self._triggers.resolve(key, ch)`; `on_complete` callback handles `submit=True` and explicit `cursor` from `TriggerResult` |
| `tui/workspace/overlays/trigger_picker.py` | Remove `Key.AT` special-case; receive pre-resolved handler; call `handler.get_lines(item, width)` per item; switch scroll to line-count model |
| `runners/tui_session.py` | `TriggerRegistry()` → `TriggerManager()` |

No new files required. No changes outside the trigger subsystem.

---

## Example: adding `!` (BashTrigger) after this PRD

```python
class BashTrigger:
    char  = "!"
    label = "Shell"

    def can_activate(self, buf): return not buf or buf[-1] == "\n"
    def get_matches(self, fragment, ctx):
        return [MatchItem(display=cmd, value=cmd, label="!", detail=cmd)
                for cmd in shell_history if cmd.startswith(fragment)]
    def on_select(self, item, fragment, buf):
        cmd = item.value if item else fragment
        return TriggerResult(buffer=buf + list("!" + cmd), submit=True)
    def on_cancel(self, fragment, buf): return buf + list("!" + fragment)
    def get_hint(self, item): return item.hint if item else None
    # get_lines: default (one line) is fine for shell commands
```

No other file changes. `submit=True` causes `unified_session` to dispatch
`SendMessageCommand` immediately after the buffer update.

## Example: adding `#` (AgentMentionTrigger) after this PRD

```python
class AgentMentionTrigger:
    char  = "#"
    label = "Agent"

    def get_matches(self, fragment, ctx):
        return [MatchItem(display=a.name, value=a.id,
                          label=f"#{a.id}", detail=a.description)
                for a in agents if a.name.startswith(fragment)]
    def on_select(self, item, fragment, buf):
        prefix = list(f"#{item.value} ")
        return TriggerResult(buffer=buf + prefix, cursor=len(buf) + len(prefix))
    # get_lines: may override to show agent description on second line
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
- [ ] `MatchItem.detail` carries the full, untruncated description for
  slash commands. No handler truncates descriptions before returning `MatchItem`.
- [ ] `SlashCommandTrigger.get_lines` wraps long descriptions onto a second
  line aligned under the description column. The full description is visible.
- [ ] The dropdown never overflows its `_MAX_VISIBLE` line budget regardless
  of how many items have multi-line display.
- [ ] All existing tests pass. `/skills`, `@mention`, and `/command` triggers
  work identically to before (description wrapping is an addition, not a
  regression).
