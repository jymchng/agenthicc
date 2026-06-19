# PRD-105 — Cross-Platform Terminal Capability Detection

## Summary

Agenthicc must support Linux, macOS, Windows, WSL, containers, CI environments, and non-interactive terminals without crashing due to unavailable terminal APIs such as `termios`.

The application shall perform capability-based terminal detection instead of platform-based assumptions.

Features that require terminal-specific functionality must degrade gracefully when unavailable.

Agenthicc must never fail startup because a terminal capability is missing.

---

## Problem Statement

Several terminal operations currently assume the availability of Unix terminal APIs.

Example:

```python
import termios
```

or

```python
termios.tcgetattr(fd)
```

These assumptions fail in environments such as:

* Windows
* Some embedded Python runtimes
* CI/CD pipelines
* IDE-integrated terminals
* Jupyter environments
* Non-TTY stdin/stdout
* Detached sessions
* Background processes

Typical failures include:

```text
ModuleNotFoundError: No module named 'termios'
```

or

```text
termios.error: (25, 'Inappropriate ioctl for device')
```

These errors prevent agenthicc from starting despite terminal functionality not being required for many operations.

---

## Failure Inventory (as-audited)

The following are the exact failure points identified in the codebase:

| File | Line(s) | Issue | Trigger |
| ---- | ------- | ----- | ------- |
| `tui/cbreak_reader.py` | 71–73 | `import termios` / `import tty` unguarded | Windows, embedded runtimes |
| `tui/cbreak_reader.py` | 88 | `old = termios.tcgetattr(fd)` unguarded | Pipe/redirect stdin on Unix |
| `tui/input/unified_session.py` | 95 | `sys.stdin.fileno()` unguarded | `io.StringIO`, some CI environments |

Already-safe code (no changes required):
- `runners/tui_session.py:_reset_terminal_on_exit()` — full `try/except` on both ANSI write and termios restore
- `tui/workspace/workspace.py` SIGWINCH handler — guarded with `try/except (AttributeError, OSError)`
- `shutil.get_terminal_size()` usages — use fallback parameter `(80, 24)`, never raise
- `_get_cols()` in workspace — wraps `os.get_terminal_size()` in `try/except OSError`

---

## Goals

### Primary Goals

* Never crash due to missing terminal APIs.
* Support all major operating systems.
* Support non-interactive environments.
* Gracefully degrade terminal features.
* Centralize capability detection.

### Non-Goals

* Emulating unavailable terminal APIs.
* Reimplementing OS-specific terminal subsystems.
* Providing full TUI functionality inside non-TTY environments.

---

## Functional Requirements

### FR-1 Capability-Based Detection

Detect terminal capabilities at runtime via `TerminalCapabilityDetector.detect()`.

Capabilities:
* `is_tty` — both stdin and stdout are real TTYs
* `supports_raw_mode` — `termios` importable and `tcgetattr` succeeds on stdin fd
* `supports_alt_screen` — `is_tty`
* `supports_colors` — `is_tty` or `COLORTERM`/`TERM` env vars indicate color support
* `supports_mouse` — `is_tty`
* `supports_resize_events` — `is_tty` and `signal.SIGWINCH` exists

### FR-2 Safe termios Import

`import termios` and `import tty` in `raw_mode()` must be guarded:

```python
try:
    import termios
    import tty
except ImportError:
    yield fd
    return
```

### FR-3 Safe Terminal Attribute Access

`termios.tcgetattr(fd)` must be guarded:

```python
try:
    old = termios.tcgetattr(fd)
except Exception:
    yield fd
    return
```

### FR-4 Non-TTY Support

`unified_session.run()` must check `sys.stdin.isatty()` before entering raw mode.
When stdin is not a TTY, return immediately without crashing.

### FR-5 Graceful Degradation

When `raw_mode` cannot set cbreak, it yields the fd without configuring the terminal.
The keyboard read loop still runs (reads line-buffered input) rather than crashing.

### FR-6 Clean Shutdown on Non-TTY

When `unified_session.run()` returns early (non-TTY), `TUISession.run()` cancels
background tasks normally via the existing `finally` block. No special shutdown logic needed.

---

## Architecture

### New: `tui/terminal_caps.py`

```python
@dataclass(frozen=True)
class TerminalCapabilities:
    is_tty: bool
    supports_raw_mode: bool
    supports_alt_screen: bool
    supports_colors: bool
    supports_mouse: bool
    supports_resize_events: bool

class TerminalCapabilityDetector:
    @classmethod
    def detect(cls) -> TerminalCapabilities: ...
```

### Modified: `tui/cbreak_reader.py:raw_mode()`

Two guards added:
1. `import termios` / `import tty` wrapped in `try/except ImportError → yield fd; return`
2. `old = termios.tcgetattr(fd)` wrapped in `try/except → yield fd; return`

Both degrade to a passthrough context manager (no raw mode configured).

### Modified: `tui/input/unified_session.py:run()`

```python
async def run(self) -> None:
    if not sys.stdin.isatty():
        return          # non-interactive — clean exit
    try:
        fd = sys.stdin.fileno()
    except Exception:
        return          # stdin has no real fd — clean exit
    ...
```

---

## Error Handling

### EH-1 Import Failures
Missing terminal modules → treated as unsupported, not fatal.

### EH-2 IOCTL Failures
`Inappropriate ioctl for device` → caught, treated as capability absent.

### EH-3 Restore Failures
Terminal restoration errors during shutdown are already caught in `_reset_terminal_on_exit`.

---

## Acceptance Criteria

* [ ] Agenthicc starts when `termios` is unavailable.
* [ ] Windows startup succeeds.
* [ ] Non-TTY execution succeeds.
* [ ] CI execution succeeds.
* [ ] Missing terminal capabilities never crash startup.
* [ ] Raw mode is only entered when supported.
* [ ] Terminal restoration never crashes shutdown.
* [ ] Capability detection is centralized in `terminal_caps.py`.
* [ ] `unified_session.run()` returns cleanly on non-TTY stdin.

---

## Non-Goals

* Emulating unavailable terminal APIs.
* Reimplementing OS-specific terminal subsystems.
* Providing full TUI functionality inside non-TTY environments.
