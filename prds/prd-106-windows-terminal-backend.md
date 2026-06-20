# PRD-106 — Windows Terminal Backend (msvcrt)

## Summary

Agenthicc implements a dedicated Windows terminal backend using `msvcrt` from
the Python standard library.  Backend selection is the **only** permitted
platform-specific branch in the codebase.

---

## Background

Windows does not provide `termios`, `tty`, or `fcntl`.  Agenthicc uses Python's
`msvcrt` standard-library module for keyboard input and console interaction on
Windows, requiring no third-party dependencies.

---

## Architecture

### Terminal Backend abstraction

```
tui/terminal/
├── __init__.py          re-exports TerminalBackend, get_backend
├── backend.py           TerminalBackend Protocol + get_backend() factory
├── posix_backend.py     wraps cbreak_reader.raw_mode / read_key
└── windows_backend.py   exclusive owner of all msvcrt calls
```

### Factory rule — the only platform branch

```python
if os.name == "nt":   → WindowsBackend   (msvcrt)
else:                 → PosixBackend     (termios / tty)
```

No other file may branch on `os.name` for terminal decisions.

---

## Functional Requirements

### FR-13 Windows Console Backend
Platform-independent `TerminalBackend` Protocol with `is_interactive()`,
`read_key()`, `enter_raw_mode()`, `restore()`.

### FR-14 Standard Library Only
`msvcrt` only.  No `pywin32`, `win32console`, `curses`, or third-party keyboard
libraries for core functionality.

### FR-15 Keyboard Input
Printable characters, Enter, Backspace, Tab, Escape, Ctrl+C, Ctrl+J, arrow
keys, Home, End, Delete.  Unicode input via `msvcrt.getwch()`.

### FR-16 Non-Blocking Peek
`msvcrt.kbhit()` used to check for pending characters before reading (used in
CSI sequence parsing — see FR-20).

### FR-17 Extended Key Translation
BIOS scan codes normalised to platform-independent `Key` values.

### FR-18 Backend Isolation
All `msvcrt` calls confined to `windows_backend.py`.

### FR-19 Graceful Capability Detection
`is_interactive()` checks `sys.stdin.isatty()`; returns `False` gracefully in
CI / redirected environments.

---

## Amendment — ConPTY / VT Sequence Support (discovered post-implementation)

### Problem

Windows terminal environments fall into two distinct input modes:

| Environment | Input format | Before fix |
|---|---|---|
| CMD, legacy PowerShell | BIOS scan codes: `\x00\x0f` → Shift+Tab | ✅ Handled |
| Windows Terminal, VS Code, new PowerShell (ConPTY) | VT sequences: `\x1b[Z` → Shift+Tab | ❌ `\x1b` → ESC, sequence lost |

**ConPTY** (the Console Pseudo Terminal layer used by all modern Windows
terminals) translates keyboard input to ANSI/VT sequences.  When `\x1b` is
received via `msvcrt.getwch()`, the original `WindowsBackend` returned
`(Key.ESC, "")` immediately, discarding the `[Z` continuation that carries the
Shift+Tab identity.

**Consequence:** Shift+Tab (mode cycling) worked on Linux but silently failed
on Windows Terminal / VS Code.  The `[` and `Z` characters then appeared in the
next read, corrupting subsequent keystrokes.

### Root cause

`windows_backend.py:89` (original):
```python
if ch == "\x1b":            return (Key.ESC, "")
```

No lookahead was performed.  Any VT escape sequence was truncated to bare ESC.

### Fix — FR-20: VT/CSI Sequence Parsing for ConPTY

When `\x1b` is received, the backend must:

1. Call `msvcrt.kbhit()` to check for pending characters.
2. If none → lone ESC → `(Key.ESC, "")` (unchanged behaviour).
3. If pending → read next character.
4. If next is `[` → CSI sequence: accumulate characters until a letter or `~` terminator.
5. Look up the completed sequence in `_CSI_KEYS` → return the mapped `Key`.
6. Unknown sequence → `(Key.ESC, "")`.

```python
# VT/ANSI CSI sequences — emitted by ConPTY environments
_CSI_KEYS: dict[str, Key] = {
    "Z":   Key.SHIFT_TAB,   # \x1b[Z  — the primary fix
    "A":   Key.UP,
    "B":   Key.DOWN,
    "C":   Key.RIGHT,
    "D":   Key.LEFT,
    "H":   Key.HOME,
    "F":   Key.END,
    "1~":  Key.HOME,
    "4~":  Key.END,
}
```

**Why `msvcrt.kbhit()` not `select.select()`:** `select` is not available on
Windows for console handles.  `kbhit()` is the Windows equivalent — returns
`True` immediately when characters are waiting in the console input buffer.
Because ConPTY delivers `\x1b[Z` as three consecutive characters already in
the buffer, `kbhit()` returns `True` instantly after reading `\x1b`.  No
timeout or blocking needed.

---

## Acceptance Criteria

| # | Requirement |
|---|---|
| 1 | Windows startup does not import `termios`. |
| 2 | Keyboard input works through `msvcrt`. |
| 3 | Unicode keyboard input works correctly. |
| 4 | Arrow keys work in both BIOS-scan and ConPTY environments. |
| 5 | Escape key works correctly (lone `\x1b` with nothing following). |
| 6 | Ctrl+C cancellation works correctly. |
| 7 | **Shift+Tab mode cycling works in CMD / legacy PowerShell (BIOS scan codes).** |
| 8 | **Shift+Tab mode cycling works in Windows Terminal / VS Code / new PowerShell (ConPTY VT sequences).** |
| 9 | Extended Windows scan codes are normalised (BIOS path unchanged). |
| 10 | VT/CSI sequences are parsed when ConPTY delivers them (`\x1b[Z`, `\x1b[A`, etc.). |
| 11 | Lone ESC (no following characters) still returns `Key.ESC`. |
| 12 | No application code imports `msvcrt`. |

---

## Files Changed

| File | Change |
|---|---|
| `tui/terminal/windows_backend.py` | Add `_CSI_KEYS` table; replace bare `\x1b → ESC` with full CSI parser using `msvcrt.kbhit()` |
