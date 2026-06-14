# PRD-57 — Input Component Architecture Revamp

## 1. Executive summary

The agenthicc TUI captures user keystrokes through two completely separate code
paths — an idle CBREAK loop (`mention_input.py`) and a streaming-mode capture task
(`streaming_input.py`) — that share no code despite performing virtually identical
terminal operations.  A third shim file (`input_area.py`) exists solely as a
redirect.  The `input_bar.py` module is misnamed: it contains completion *data
types*, not the visual input bar.

The consequence is compounded drift: bugs fixed in one path silently reappear in
the other; new features must be written twice; and the single monolithic
`read_line_with_mention` function that housed 700+ lines of interleaved state was
impossible to unit-test without patching six internal symbols.

PRD-57 specifies a clean, unified `tui/input/` package that:
* shares all CBREAK plumbing between idle and streaming modes,
* makes every state object independently testable,
* gives the cursor and rendering a single authoritative implementation,
* and eliminates the shim files.

---

## 2. Problem statement

### 2.1 Dual CBREAK loops

`mention_input.py` sets up CBREAK via `_raw_mode(fd)` and reads keys via
`_read_key(fd)`.  `streaming_input.py` duplicates the CBREAK setup inline with
its own `termios` / `tty` / `os.read` calls.

When a CBREAK bug is fixed in one place the other regresses.  This happened
repeatedly during the PRD-56 work: the `ICRNL` clearing, the `ISIG` suppression,
and the bracketed-paste enable/disable sequence all had to be fixed twice.

### 2.2 Monolithic state machine

The original `read_line_with_mention` was a single 700-line function containing:

| Concern | Lines (approx.) |
|---|---|
| Normal editing (buf, cursor) | ~120 |
| Paste condensation | ~80 |
| History navigation | ~60 |
| Trigger mode (active_handler, fragment, matches) | ~200 |
| Mode cycling | ~20 |
| Exit / Ctrl+C handling | ~40 |
| Rendering calls scattered through the loop | ~80 |
| Other | ~100 |

State was stored in mutable single-element lists (`_paste_condensed: list[bool] =
[False]`) to work around Python's `nonlocal` syntax.  The key dispatch was a
flat `if/elif` chain ~300 lines long that handled both trigger-mode and normal-mode
keys in the same block, forcing every reader to mentally track which branch they
were in.

### 2.3 Misnamed / misplaced modules

`input_bar.py` contains `CommandSpec`, `CommandRegistry`, and `AtMentionCompleter`
— data types for completions, not the visual input bar.  `input_area.py` contains
only a re-export pointing to `mention_input.py`.  Both names mislead readers about
where to find and change things.

### 2.4 Test coupling to internal symbols

Tests bypass the public API to patch `mention_input._raw_mode`, `._read_key`,
and `._redraw`, creating a brittle dependency on implementation detail.  Any
refactor that moves these symbols breaks the entire test matrix even when behaviour
is unchanged.

### 2.5 Streaming trigger havoc (PRD-56 regression)

Attempting to show the @-mention picker during a streaming agent turn required
pausing the Rich Live block and starting a new `raw_mode` context on the same
stdin fd.  The nested contexts left the terminal cursor visible, bracketed paste
disabled, and the live panel in an undefined state — the "havoc" the user reported.
The root cause is that no clean handoff protocol existed between idle and streaming
input modes.

---

## 3. Goals

| # | Goal |
|---|---|
| G1 | Single CBREAK primitive shared by both idle and streaming modes |
| G2 | Single `InputBuffer` with typed mutation methods; no parallel list-of-chars |
| G3 | Separate, independently testable state objects for paste, history, and trigger |
| G4 | Single `PromptRenderer` for ANSI output; streaming mode uses `InputBarState` for Rich live-panel output |
| G5 | `match`-based key dispatch — one dispatch method per mode, zero interleaving |
| G6 | `_EXIT` sentinel pattern: `None` means "keep looping", `_EXIT` means "exit returning None" |
| G7 | Delete `mention_input.py`, `input_area.py` as real files; keep thin re-export shims during transition |
| G8 | Rename `input_bar.py` → `input/completions.py` with backward-compat re-export |
| G9 | Streaming trigger handoff: clean pause/resume of Live block with no nested raw_mode |
| G10 | No test changes required during migration; all existing tests continue to pass |

---

## 4. Current file map

```
tui/
  cbreak_reader.py          Key, raw_mode, read_key                      KEEP (already extracted)
  input_area.py             re-export shim → mention_input               DELETE (Phase 3)
  input_bar.py              CommandSpec, Registry, AtMentionCompleter     RENAME (Phase 2)
  mention_input.py          backward-compat shim + _read_key impl        THIN (Phase 2), DELETE (Phase 3)
  streaming_input.py        streaming CBREAK capture (asyncio task)      REFACTOR (Phase 2)

  input/
    __init__.py             package exports                               DONE
    buffer.py               InputBuffer                                   DONE
    history.py              HistoryNavigator                              DONE
    paste.py                PasteState                                    DONE
    renderer.py             PromptRenderer                                DONE
    session.py              InputSession (idle mode)                      DONE (needs cleanup)
```

---

## 5. Target file map

```
tui/
  cbreak_reader.py          Key, raw_mode, read_key                      unchanged

  input/
    __init__.py             package exports
    buffer.py               InputBuffer
    history.py              HistoryNavigator
    paste.py                PasteState
    completions.py          CommandSpec, CommandRegistry, AtMentionCompleter
                            (was input_bar.py)
    renderer.py             PromptRenderer  (ANSI idle output)
    session.py              IdleInputSession
    streaming.py            StreamingSession  (asyncio task, was streaming_input.py)

  # Shim files kept for backward compat during transition:
  input_bar.py              re-export → input/completions.py
  mention_input.py          re-export → input/session.py  +  real _read_key impl
  input_area.py             re-export → input/renderer.py
  streaming_input.py        re-export → input/streaming.py
```

During Phase 3 the shim files become one-line re-exports.  They are deleted only
after all internal callers and tests are updated.

---

## 6. Component specifications

### 6.1 `cbreak_reader.py` — terminal primitives

**Status**: done; no changes required.

Exports: `Key` (str Enum), `raw_mode(fd)` (contextmanager), `read_key(fd)` (→
`tuple[Key, str]`).

Design invariant: `raw_mode` saves the full `termios` state *after* `setcbreak`
(not before) so subsequent patches (`ICRNL`, `ISIG`, `ECHOCTL`) layer on top of
CBREAK rather than replacing it.  Nested calls on the same fd are safe: each call
saves and restores independently.

### 6.2 `input/buffer.py` — `InputBuffer`

**Status**: done; no changes required.

Pure value object.  No I/O, no callbacks.  Cursor is always in `[0, len(buf)]`.

```python
class InputBuffer:
    buf: list[str]     # read-only property
    cursor: int        # read-write, clamped automatically

    # mutations
    def insert(ch)              # insert at cursor, advance cursor
    def insert_many(chars) -> tuple[int, int]   # returns (start, end)
    def delete_before()         # backspace
    def delete_range(start, end)
    def set(chars, cursor=None) # replace entire buf
    def clear()

    # navigation — return False when no movement possible (caller falls through)
    def move_left() / move_right() / move_home() / move_end()
    def move_up() -> bool
    def move_down() -> bool
```

All navigation methods clamp silently; callers never need to bounds-check.

### 6.3 `input/paste.py` — `PasteState`

**Status**: done; no changes required.

```python
@dataclass
class PasteState:
    condensed: bool = False
    label: str = ""
    start: int = 0
    end: int = 0
    count: int = 0

    def apply(buf, text, cols)  # insert text, condense if large
    def expand()                # Ctrl+V
    def backspace(buf)          # delete entire paste block
```

Condensation threshold: `n_lines > 3` OR `len(text) > max(cols - 4, 40)`.

### 6.4 `input/history.py` — `HistoryNavigator`

**Status**: done; no changes required.

Wraps a shared `list[str]` with up/down navigation and a saved-current snapshot.
`up()` and `down()` return `list[str] | None`; `None` means no further navigation.
`commit(text)` appends and resets the index.

### 6.5 `input/renderer.py` — `PromptRenderer`

**Status**: functionally complete; minor refinements in Phase 2.

Owns all ANSI terminal writes for the *idle* mode prompt.  `StreamingSession` does
not use `PromptRenderer`; it calls `input_bar_state.update()` instead (the Rich
live panel owns the streaming prompt).

```python
class PromptRenderer:
    def render(buf, cursor, dropdown, mode_line) -> int
    # Returns total rows written below the input line (footer + dropdown).
    # Caller stores this as prev_n_lines for the next render's erase step.

    def scrub_cursor(buf)       # rewrite prompt without ▌ before submit
    def erase_below(n_rows)     # step past input rows, erase footer/dropdown, step back
    def show_exit_hint(resume_id)

# Module-level helpers (used by _redraw backward-compat shim):
def build_prompt(buf, cursor, mention_suffix, in_trigger) -> str
def build_footer(mode_str, cols) -> tuple[str, str]
```

**Rendering invariant**: after `render()` returns, the terminal cursor is on the
*first* input row.  This is required by `erase_below()` / `scrub_cursor()` which
both start by moving down from the current row.

**Width safety**: every output line is clamped to `cols` via `_truncate(text,
max_cols)` before writing.  Lines that exceed `cols` cause Rich's cursor-tracking
to desynchronise (the live-panel-havoc bug described in PRD-56).

### 6.6 `input/session.py` — `IdleInputSession`

**Status**: working; needs the cleanup described in §7.

The idle input loop.  Runs in a thread via `asyncio.to_thread(read_line_with_mention, ...)`.

```
State objects
─────────────
  InputBuffer           buf + cursor
  PasteState            paste condensation
  HistoryNavigator      up/down history
  _TriggerState         active handler + fragment + matches (or None)
  MenuDriver            open menu overlay (or None)
  PromptRenderer        all ANSI writes
  _ctrl_c_count         int
  _mode_notification    Any | None

Injectable callables (for test patchability)
────────────────────────────────────────────
  _fn_raw_mode          default: cbreak_reader.raw_mode
  _fn_read_key          default: cbreak_reader.read_key
  _fn_render            default: self._render   (calls PromptRenderer)
```

**Key dispatch**:

```python
def run() -> str | None:
    with _fn_raw_mode(fd):
        while True:
            _do_render()
            key, ch = _fn_read_key(fd)
            if driver.active:
                driver.handle_key(key, ch); continue
            ret = _dispatch_trigger(key, ch) if trigger else _dispatch_normal(key, ch)
            if ret is _EXIT: return None
            if ret is not None: return ret

def _dispatch_normal(key, ch) -> object:
    match key:
        case Key.ENTER:      return _submit()
        case Key.CTRL_D:     return _ctrl_d()
        case Key.CTRL_C:     return _ctrl_c()       # first: clear; second: _EXIT
        case Key.CTRL_ENTER: buf.insert("\n")
        case Key.LEFT:       buf.move_left()
        case Key.RIGHT:      buf.move_right()
        case Key.HOME:       buf.move_home()
        case Key.END:        buf.move_end()
        case Key.UP:         _history_up()
        case Key.DOWN:       _history_down()
        case Key.BACKSPACE:  _backspace()
        case Key.CTRL_U:     buf.clear()
        case Key.CTRL_V:     paste.expand()
        case Key.SHIFT_TAB:  _cycle_mode()
        case Key.PASTE:      _apply_paste(ch)
        case Key.AT:         _activate_trigger("@")
        case Key.CHAR:       _insert_or_trigger(ch)
    return None   # continue looping

def _dispatch_trigger(key, ch) -> object:
    match key:
        case Key.CTRL_C:        _cancel(); first→None, second→_EXIT
        case Key.ESC:           _cancel()
        case Key.ENTER | Key.TAB: return _select()
        case Key.UP:            trigger.selected -= 1
        case Key.DOWN:          trigger.selected += 1
        case Key.BACKSPACE:     _trigger_backspace()
        case Key.AT | Key.CHAR: trigger.fragment += ch
    return None
```

**Sentinel**: `_EXIT = object()` — the only value that causes `run()` to `return None`.
Plain `None` from a dispatch method means "keep looping".  This eliminates the
class of bug where Ctrl+D on empty buffer or double Ctrl+C caused an infinite loop.

**Patchability contract**: `read_line_with_mention` in `mention_input.py` creates
an `IdleInputSession` and then sets:
```python
session._fn_raw_mode = _mi._raw_mode    # patchable at mention_input level
session._fn_read_key = _mi._read_key    # patchable at mention_input level
session._fn_render   = _render_via_redraw   # calls _mi._redraw which is patchable
```

Tests patching `mention_input._raw_mode` etc. continue to work unchanged.

### 6.7 `input/streaming.py` — `StreamingSession`

**Status**: exists as `streaming_input.py`; needs refactor in Phase 2.

Runs as an asyncio background task during agent turns.  Simpler than
`IdleInputSession`: no trigger system, no history, no paste condensation.

```python
class StreamingSession:
    def __init__(self, input_bar_state, pending_queue, console, live_panel=None): ...

    def start() -> None    # create asyncio task
    def stop() -> None     # cancel task + clear state

    async def _run() -> None:
        with raw_mode(fd):
            while True:
                await asyncio.sleep(0.02)
                # non-blocking select; read one byte
                key, ch = _read_streaming_key(fd)
                match key:
                    case Key.ENTER:      _submit()
                    case Key.CTRL_ENTER: buf.insert("\n")
                    case Key.BACKSPACE:  buf.delete_before()
                    case Key.CTRL_U:     buf.clear()
                    case Key.CHAR:       buf.insert(ch)
                _push()    # → input_bar_state.update(buf.buf, buf.cursor)
```

**Using `InputBuffer`**: replaces the bare `list[str]` in the current implementation.
`buf.cursor` is always valid; no manual `len(self._buf)` tracking.

**Streaming trigger (P2)**: Trigger picker during streaming requires a clean handoff:
```
user types @  →  StreamingSession signals want_trigger=True
                 →  outer loop stops StreamingSession
                 →  live_panel.stop()
                 →  IdleInputSession.run(initial_buf=current_buf)
                 →  live_panel.start()
                 →  StreamingSession restarts with completed buf
```

This avoids nested `raw_mode` contexts on the same fd.  Implemented as a
`want_trigger: asyncio.Event` on `StreamingSession`; the outer `AgenthiccTUI.run()`
loop awaits it alongside the agent coroutine.

### 6.8 `input/completions.py` — completion data types

**Status**: exists as `input_bar.py`; rename in Phase 2.

No functional changes.  Exports:
- `CommandSpec`, `BUILTIN_COMMANDS`
- `SlashCommandCompleter`, `CommandRegistry`, `build_default_registry`
- `_entry_meta`, `AtMentionCompleter`

`input_bar.py` becomes a one-line re-export shim:
```python
from agenthicc.tui.input.completions import *  # noqa: F401, F403
```

---

## 7. Gaps to close in the current `input/session.py`

The implementation completed so far (PRD-56 + PRD-57 Phase 1) is functional and
all tests pass, but has several issues to resolve in Phase 2:

| # | Gap | Impact |
|---|---|---|
| G1 | `_render_via_redraw` closure in `mention_input.read_line_with_mention` adds complexity every call | Maintenance burden; should move into `IdleInputSession.__init__` |
| G2 | `_mode_line()` imports `_truncate` but never uses it | Dead import |
| G3 | Trigger-mode `Key.CTRL_C` second press duplicates erase-below logic from `_handle_ctrl_c` | DRY violation |
| G4 | `StreamingSession` still uses a bare `list[str]` for the buffer | Should use `InputBuffer` for shared semantics |
| G5 | `_fn_render` set externally by `read_line_with_mention` | Should be a constructor parameter for clarity |
| G6 | `avail` variable computed in `render_prompt` but never used (`_ = avail`) | Remove |
| G7 | `streaming_input.py` has unreachable `_open_trigger_picker` code path | Remove; replace with `want_trigger` event (P2) |

---

## 8. Migration plan

### Phase 1 — Foundation (DONE)

- [x] `cbreak_reader.py`: extract `Key`, `raw_mode`, `read_key`
- [x] `input/buffer.py`: `InputBuffer`
- [x] `input/paste.py`: `PasteState`
- [x] `input/history.py`: `HistoryNavigator`
- [x] `input/renderer.py`: `PromptRenderer` (returns `int`)
- [x] `input/session.py`: `IdleInputSession` with `match` dispatch + `_EXIT` sentinel
- [x] `mention_input.py`: shim with patchable `_fn_raw_mode`, `_fn_read_key`, `_fn_render`
- [x] All 1687 tests pass

### Phase 2 — Consolidation

**P2-1: Rename `input_bar.py` → `input/completions.py`**
- Create `input/completions.py` with the full implementation
- Replace `input_bar.py` with a one-line re-export shim
- No test changes (tests import from `input_bar` which re-exports)

**P2-2: Refactor `streaming_input.py` → `input/streaming.py`**
- Move `StreamingInput` to `input/streaming.py`, renamed `StreamingSession`
- Replace bare `list[str]` buffer with `InputBuffer`
- Use `read_key(fd)` from `cbreak_reader` instead of manual `os.read` loop
- Replace `input_bar_state._buf` push with typed `InputBuffer` → `input_bar_state.update(buf.buf, buf.cursor)`
- Replace `streaming_input.py` with re-export shim
- Add `want_trigger: asyncio.Event` for P2-4

**P2-3: Clean up `input/session.py`**
- Move `_fn_render` setup into `IdleInputSession.__init__` (constructor param)
- Remove dead `avail` variable in `render_prompt`
- Remove `_mode_line()` unused import
- Deduplicate trigger Ctrl+C double-press logic

**P2-4: Streaming trigger handoff (optional, P2 priority)**
- Add `want_trigger: asyncio.Event` to `StreamingSession`
- Update `AgenthiccTUI.run()` to await trigger event alongside agent coroutine
- On trigger: `streaming.stop()` → `live_panel.stop()` → `IdleInputSession.run(initial_buf=buf)` → `live_panel.start()` → `streaming.start(initial_buf=result)`

### Phase 3 — Cleanup (after all internal callers updated)

- Update test fixtures to patch at `input.session` level instead of `mention_input`
- Delete `mention_input.py` (replace with 3-line re-export: `Key`, `read_line_with_mention`)
- Delete `input_area.py`
- Update all `from agenthicc.tui.mention_input import ...` → `from agenthicc.tui.input.session import ...`
- Update all `from agenthicc.tui.input_bar import ...` → `from agenthicc.tui.input.completions import ...`

---

## 9. Data flow diagrams

### 9.1 Idle mode (single agent turn)

```
AgenthiccTUI.run()
  │
  ├─ _print_idle_status()              [Rich console.print → stdout]
  │
  ├─ asyncio.to_thread(
  │     read_line_with_mention, ...)
  │       │
  │       ├─ IdleInputSession.__init__(registry, history, mode_manager, ...)
  │       │     InputBuffer, PasteState, HistoryNavigator, PromptRenderer
  │       │
  │       └─ session.run()
  │             │
  │             ├─ with raw_mode(fd):
  │             │     while True:
  │             │       _do_render()           ← PromptRenderer.render()
  │             │       key, ch = read_key(fd) ← blocks on stdin
  │             │       dispatch(key, ch)      ← match statement
  │             │           ↓ Enter
  │             │       return "text"
  │             │
  │             └─ returns "text" or None
  │
  ├─ on_intent_submitted()
  ├─ live_panel.start()
  ├─ streaming_session.start()         [asyncio background task]
  │
  ├─ await _run_agent(on_input("text"))
  │     agent publishes events → TUI bus → live_panel redraws
  │
  ├─ streaming_session.stop()
  ├─ live_panel.stop()
  └─ _flush_new_lines()
```

### 9.2 Streaming mode key dispatch

```
StreamingSession._run()
  │
  └─ with raw_mode(fd):
        while True:
          await sleep(0.02)
          if not select(fd, timeout=0): continue
          key, ch = _read_streaming_key(fd)    ← subset of read_key, non-blocking

          match key:
            ENTER     → queue text; buf.clear()
            CTRL_J    → buf.insert("\n")
            BACKSPACE → buf.delete_before()
            CTRL_U    → buf.clear()
            CHAR      → buf.insert(ch)
            ESC_SEQ   → skip

          input_bar_state.update(buf.buf, buf.cursor)
          └─ ReactiveProperty → LivePanel._redraw() → Rich Live update
```

### 9.3 InputBuffer mutation semantics

```
                  cursor
                    ↓
buf = ['h','e','l','l','o']
                    │
insert('X')  →  ['h','e','l','X','l','o'], cursor = 4
                       ↑
                    cursor
delete_before() →  ['h','e','l','l','o'], cursor = 3
move_left()    →  cursor = 3
move_right()   →  cursor = 4 (clamped to len)
move_home()    →  cursor = 0 (on this logical line)
move_end()     →  cursor = 5 (on this logical line)
move_up()      →  returns False (single-line), caller navigates history
```

---

## 10. Test strategy

### 10.1 What tests exist (passing)

| Test module | What it covers |
|---|---|
| `test_mention_input.py` | `_get_matches`, `_read_key`, `_find_trigger_tail`, state machine via `read_line_with_mention` |
| `test_trigger_integration.py` | Trigger activation, selection, cancellation, backspace re-entry |
| `test_menu_integration.py` | Initial menu open/close, menu edit+confirm |
| `test_mode_ui.py` | `_redraw` row count with and without dropdown |
| `test_input_bar.py` | `CommandRegistry`, `SlashCommandCompleter`, `AtMentionCompleter` |
| `test_mention_ui.py` | `_entry_meta`, `AtMentionCompleter.completions` |

All 1687 tests pass at the end of Phase 1.

### 10.2 New unit tests for Phase 2

**`test_input_buffer.py`** (new):
- `insert` advances cursor correctly; multi-byte char
- `delete_before` at position 0 is no-op
- `delete_range` clamps cursor
- `move_up` returns False on first line, True on second
- `move_down` returns False on last line
- `move_home` / `move_end` within multi-line text

**`test_paste_state.py`** (new):
- Short paste: not condensed
- Long paste (> 3 lines): condensed, label set
- Wide paste (> cols - 4 chars): condensed
- `expand()` clears `condensed`
- `backspace()` deletes entire range from `InputBuffer`

**`test_history_navigator.py`** (new):
- `up()` saves current, returns previous entry
- `up()` at oldest returns None
- `down()` returns saved current
- `commit()` resets index

**`test_idle_input_session.py`** (new — tests via injectable callables, no TTY):
- Normal char insert: cursor advances
- Backspace: cursor retreats
- Enter with text: returns text, history updated
- Ctrl+D empty: returns None
- Ctrl+D non-empty: returns text
- Ctrl+C once: clears buffer, continues
- Ctrl+C twice: returns None (exits)
- Up/Down: history navigation
- Ctrl+Enter: newline inserted
- Left/Right/Home/End: cursor moves
- Trigger @: activates AtMentionTrigger
- Trigger Enter: selects and closes
- Trigger Esc: cancels, restores literal char

**`test_streaming_session.py`** (new):
- Enter queues stripped text, clears buffer
- Ctrl+J inserts newline
- Backspace deletes
- Ctrl+U clears
- Printable char appended
- UTF-8 multi-byte char decoded correctly
- `stop()` cancels task

### 10.3 Phase 3 test migration

Replace all `patch("agenthicc.tui.mention_input._raw_mode", ...)` with
`patch("agenthicc.tui.input.session.IdleInputSession._fn_raw_mode", ...)` (or
pass the fake via constructor).

This makes tests independent of the shim files so Phase 3 deletion is safe.

---

## 11. Acceptance criteria

### Phase 2

- [ ] `input/completions.py` exists; `input_bar.py` is a re-export shim
- [ ] `input/streaming.py` exists; `streaming_input.py` is a re-export shim
- [ ] `StreamingSession` uses `InputBuffer` (no bare `list[str]`)
- [ ] `StreamingSession` uses `read_key` from `cbreak_reader` (no manual `os.read`)
- [ ] `_render_via_redraw` closure removed from `mention_input.read_line_with_mention`
- [ ] All 1687 existing tests pass
- [ ] New `test_input_buffer.py`, `test_paste_state.py`, `test_history_navigator.py` pass

### Phase 3

- [ ] `mention_input.py` is ≤ 5 lines (pure re-export)
- [ ] `input_area.py` deleted
- [ ] No internal source file imports from `mention_input` (only external/test compat)
- [ ] No `input_bar` imports in non-test, non-shim source files
- [ ] All tests pass

---

## 12. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| `raw_mode` nesting in streaming trigger handoff causes terminal corruption | Medium | `StreamingSession.stop()` fully exits its raw_mode before `IdleInputSession` starts; guarded by asyncio.Event signalling |
| `_EXIT` sentinel leaks outside `session.py` causing `AttributeError` in callers | Low | Sentinel is module-private; `run()` converts it to `None` before returning |
| Test fragility during Phase 3 migration | Medium | Migrate tests file-by-file; keep shims until all imports are updated |
| `PromptRenderer` and `InputBarState.render_prompt` diverge | Low | Both use `InputBuffer.buf` + `InputBuffer.cursor`; write a shared `build_prompt()` function (already done in `renderer.py`) |
| `StreamingSession` missing CBREAK flags (ICRNL, ISIG) | Low | `raw_mode` from `cbreak_reader` applies all flags; no per-module termios code |

---

## 13. Prioritised roadmap

| Priority | Item | Phase |
|---|---|---|
| **P0** | `_EXIT` sentinel (already done — fixed timeout + can't-type-after-first-response) | 1 ✓ |
| **P0** | `PromptRenderer.render()` returns row count (already done — fixes _redraw tests) | 1 ✓ |
| **P1** | `input/completions.py` + `input_bar.py` shim | 2 |
| **P1** | `input/streaming.py` + `InputBuffer` | 2 |
| **P1** | `IdleInputSession` cleanup (G1–G7 from §7) | 2 |
| **P1** | New unit tests: buffer, paste, history, session | 2 |
| **P2** | Streaming trigger handoff via `want_trigger` event | 2 |
| **P3** | Delete `mention_input.py` (full shim) + update tests | 3 |
| **P3** | Delete `input_area.py` | 3 |
| **P4** | IDE completion / LSP improvements from cleaner imports | 3 |
