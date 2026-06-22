# PRD-127 — Windows Shift+Tab Mode Cycling Fix

## Problem

On Windows, Shift+Tab does not cycle the operational mode.  The keystroke is
never delivered to `ModeCycleCapability` because `WindowsBackend.read_key()`
never produces `Key.SHIFT_TAB`.

### Root cause

`WindowsBackend.read_key()` reads input via `msvcrt.getwch()`.  `getwch()` calls
the CRT `_getwch`, which reads **translated console key events** (the legacy
two-byte `\x00`/`\xe0` + scan-code encoding) — it does **not** read the raw VT
input byte stream.

The backend had two Shift+Tab decode paths, both unreachable in practice:

1. **BIOS `\x00\x0f`** (`_EXT_00`): not delivered for Shift+Tab under ConPTY
   (Windows Terminal / VS Code / modern PowerShell) — the original bug.
2. **VT `\x1b[Z`** (`_CSI_KEYS`, added by the PRD-106 amendment): a terminal
   only emits `\x1b[Z` on its input pipe when the console input handle has
   `ENABLE_VIRTUAL_TERMINAL_INPUT` set **and** raw bytes are read
   (`ReadFile`/`ReadConsole`).  `getwch()` does neither, and the codebase never
   sets that mode (verified: no `SetConsoleMode` / `ENABLE_VIRTUAL_TERMINAL_INPUT`
   anywhere).  So this path is dead under `getwch()`.

Net effect: Shift+Tab collapses to `Key.TAB` (or an undelivered scan code),
which no idle/streaming capability consumes, so nothing happens.

The downstream chain is correct: `get_backend()` selects `WindowsBackend`,
`UnifiedInputSession.run()` calls `backend.read_key()`, and
`ModeCycleCapability` fires on `Key.SHIFT_TAB`.  Only the decode is broken.

## Decision — `ReadConsoleInputW` via `ctypes`

Replace the `getwch()` read with the low-level console API
`ReadConsoleInputW`, which returns `KEY_EVENT_RECORD` structures carrying
`wVirtualKeyCode` **and** `dwControlKeyState`.  Shift+Tab is then
`VK_TAB (0x09)` with `SHIFT_PRESSED (0x0010)` set — unambiguous.

Chosen over enabling `ENABLE_VIRTUAL_TERMINAL_INPUT` + raw-byte reads because:

- **Robust across all hosts** — works on legacy CMD, ConPTY, Windows Terminal,
  VS Code; no dependency on terminal VT translation.
- **Directly exposes modifiers** — no ambiguity between Tab and Shift+Tab.
- **Testable on Linux CI** — the decode logic is a pure function over
  `(virtual_key, unicode_char, control_state)`; the only Windows-specific part
  is the thin `ReadConsoleInputW` reader, isolated behind one method that tests
  monkeypatch.  (The VT-input approach can't be validated without a Windows
  console because `getwch()` cannot read the VT byte stream.)
- **Self-contained** — all `ctypes`/console-API code stays in
  `windows_backend.py`, honouring the "only this file does Windows terminal
  I/O" rule.

## Solution

### 1. Pure decoder — `_decode_key_event(vk, unicode_char, ctrl_state)`

Module-level, no Windows API calls (importable + testable on Linux).  Returns
`tuple[Key, str] | None` (`None` = ignore: modifier-only / key we don't map).

Order: **virtual-key checks first** (so Shift+Tab is caught via `VK_TAB` +
`SHIFT_PRESSED` before the `UnicodeChar` fallback), then control-character and
printable `UnicodeChar` handling.

| Condition | Result |
|---|---|
| `VK_TAB` + SHIFT | `SHIFT_TAB` |
| `VK_TAB` | `TAB` |
| `VK_RETURN` + CTRL | `CTRL_ENTER` |
| `VK_RETURN` | `ENTER` |
| `VK_BACK` | `BACKSPACE` |
| `VK_ESCAPE` | `ESC` |
| `VK_UP/DOWN/LEFT/RIGHT/HOME/END` | arrows / `HOME` / `END` |
| `UnicodeChar` 0x03/0x04/0x15/0x16 | `CTRL_C` / `CTRL_D` / `CTRL_U` / `CTRL_V` |
| `UnicodeChar` `@` | `AT` |
| printable `UnicodeChar` | `CHAR` |
| otherwise | `None` |

### 2. ctypes structures — portable type objects

`_COORD`, `_KEY_EVENT_RECORD`, `_INPUT_RECORD` defined with `ctypes.c_short /
c_ushort / c_int / c_wchar / c_ulong` (NOT `ctypes.wintypes`, which only imports
on Windows).  This keeps the module importable on Linux for testing.

### 3. `_read_key_console()` + `_next_input_event()`

`_next_input_event()` performs the single `ReadConsoleInputW` call and returns
`(event_type, key_down, vk, unicode_char, ctrl_state)` (or `None`).
`_read_key_console()` loops, skipping non-`KEY_EVENT` records, key-up events,
and `None` decodes, until `_decode_key_event` yields a key.  Tests monkeypatch
`_next_input_event` to drive the loop with fabricated events.

### 4. Raw console input mode in `enter_raw_mode()`

Save the current input mode, clear `ENABLE_LINE_INPUT`, `ENABLE_ECHO_INPUT`,
and `ENABLE_PROCESSED_INPUT` (so Ctrl+C arrives as a key event, mirroring the
POSIX `ISIG`-clear), restore on exit.  Keep the existing output VT writes
(cursor hide + bracketed paste) and the stdout flush that makes the input bar
visible.

### 5. Fallback

If the console handle / `GetConsoleMode` is unavailable (redirected stdin, not
a real console), `read_key()` falls back to the existing `getwch()` decode so
no environment regresses.

## Acceptance criteria

| # | Criterion |
|---|---|
| 127.1 | `_decode_key_event(VK_TAB, "", SHIFT_PRESSED)` → `(Key.SHIFT_TAB, "")` |
| 127.2 | `_decode_key_event(VK_TAB, "\t", 0)` → `(Key.TAB, "")` |
| 127.3 | `VK_RETURN` with/without CTRL → `CTRL_ENTER` / `ENTER` |
| 127.4 | Arrows, HOME, END, BACKSPACE, ESC decode by virtual key |
| 127.5 | `UnicodeChar` controls → `CTRL_C/CTRL_D/CTRL_U/CTRL_V`; `@` → `AT`; printable → `CHAR` |
| 127.6 | Modifier-only / unmapped key → `None` |
| 127.7 | `_read_key_console()` skips key-up + non-key events and returns the first decodable key |
| 127.8 | Module imports cleanly on Linux (no `ctypes.wintypes` at import time) |
| 127.9 | `getwch()` fallback preserved when no console handle |

## Files changed

| File | Change |
|---|---|
| `src/agenthicc/tui/terminal/windows_backend.py` | `_decode_key_event`, ctypes structs, `_read_key_console` / `_next_input_event`, raw input-mode setup, getwch fallback |
| `tests/unit/test_windows_terminal_backend.py` | New — decoder + read-loop coverage |
| `AGENTS.md` | Update terminal backend rules |
| `prds/prd-68-feature-expectations.md` | Add §41 |
