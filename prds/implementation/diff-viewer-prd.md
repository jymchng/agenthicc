# Diff Viewer — Implementation PRD

**Document ID:** diff-viewer-prd  
**Status:** Implementation-ready  
**Consuming agents:** autonomous coding agents with no external clarification available  
**Parent PRD:** prds/tui-redesign-prd.md (master TUI redesign)  
**Related:** prds/visual-design-system-research.md, prds/ai-coding-agent-ux-research.md, prds/component-inventory.md

---

## 0. Context and Constraints

### 0.1 Architecture Context

AgentHICC uses a committed-transcript + live-bottom-block pattern. Completed turns
are printed once to stdout and scroll permanently into the terminal native scrollback.
Diffs are always committed content — they appear inline in the transcript and are
NEVER rendered in the bottom block or an alternate screen.

The existing codebase has a proto-implementation in
`src/agenthicc/__main__.py` (lines 326–422) that captures file snapshots before
`write_file`/`patch_file` calls and computes `difflib.unified_diff` on completion.
This PRD supersedes and replaces that ad-hoc implementation with a properly
structured module.

Relevant existing symbols (do NOT duplicate):
- `_file_snapshots: dict[str, tuple[str, str]]` pattern in `__main__.py` — replace
  with `FileSnapshotStore` class defined here.
- `transcript.finish_tool_call(tool_use_id=tid, output=diff)` — remains the call
  site; the diff string format produced here must be consumable by that method.

### 0.2 Hard Constraints

1. **No alternate screen** — all diff rendering is committed to scrollback via
   `terminal.commit_lines()` or equivalent transcript APIs. No `\x1b[?1049h`.
2. **Python only** — no Node, no Rust, no subprocess for diff generation.
3. **`difflib.unified_diff` only** — do not use `git diff`, `diff(1)`, or any
   external binary. Diff computation is pure Python.
4. **Rich for rendering** — use `rich.text.Text` and `rich.console.Console` for
   ANSI output assembly. Do not hand-roll ANSI escape sequences in rendering code.
5. **Full type hints, mypy clean** — every function and class must have complete
   type annotations. `from __future__ import annotations` on every file.
6. **`asyncio_mode = "auto"` in pytest** — no `@pytest.mark.asyncio` decorators.
7. **Test markers** — `@pytest.mark.unit`, `@pytest.mark.integration`,
   `@pytest.mark.e2e`.

---

## 1. Diff Data Model

### 1.1 Module Location

```
src/agenthicc/tui/diff_model.py
```

### 1.2 DiffLineType Enum

```python
from __future__ import annotations

from enum import Enum


class DiffLineType(Enum):
    CONTEXT = "context"    # unchanged context line
    ADDED   = "added"      # line added in new version
    REMOVED = "removed"    # line removed from old version
    HEADER  = "header"     # hunk header line (@@ -a,b +c,d @@)
    FILE_HEADER = "file_header"  # --- a/path or +++ b/path line
```

### 1.3 DiffLine Dataclass

```python
from __future__ import annotations

from dataclasses import dataclass

from agenthicc.tui.diff_model import DiffLineType


@dataclass(frozen=True)
class DiffLine:
    content: str              # raw line text INCLUDING leading +/-/ char
    line_type: DiffLineType
    line_number_old: int | None  # 1-based; None for ADDED and HEADER lines
    line_number_new: int | None  # 1-based; None for REMOVED and HEADER lines
```

Invariants:
- `line_number_old` is `None` for `ADDED`, `HEADER`, and `FILE_HEADER` lines.
- `line_number_new` is `None` for `REMOVED`, `HEADER`, and `FILE_HEADER` lines.
- `content` is the raw diff line exactly as produced by `difflib.unified_diff`
  (e.g., `"+    return await verify(token)"`).
- `content` for `CONTEXT` lines begins with a single space: `" unchanged"`.
- `content` for `HEADER` lines begins with `"@@"`.
- `content` for `FILE_HEADER` lines begins with `"---"` or `"+++"`.

### 1.4 DiffHunk Dataclass

```python
from __future__ import annotations

from dataclasses import dataclass, field

from agenthicc.tui.diff_model import DiffLine


@dataclass(frozen=True)
class DiffHunk:
    header: str           # raw @@ line text, e.g. "@@ -85,7 +85,7 @@ async def verify_jwt"
    start_line_old: int   # first line number in old file (1-based)
    start_line_new: int   # first line number in new file (1-based)
    count_old: int        # number of lines from old file in this hunk
    count_new: int        # number of lines from new file in this hunk
    lines: tuple[DiffLine, ...]  # ordered diff lines; tuple for immutability
    additions: int        # count of ADDED lines in this hunk
    deletions: int        # count of REMOVED lines in this hunk
```

### 1.5 DiffResult Dataclass

```python
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DiffResult:
    tool_use_id: str          # correlation ID matching the tool call
    file_path: str            # relative path, e.g. "src/auth/session.py"
    original: str             # full original file content (before write)
    modified: str             # full modified file content (after write)
    hunks: tuple[DiffHunk, ...]  # parsed hunks; empty if no changes
    total_additions: int      # sum of additions across all hunks
    total_deletions: int      # sum of deletions across all hunks
    is_binary: bool           # True if file failed UTF-8 decode
    size_bytes: int           # size of modified file in bytes
    context_lines: int        # context lines used when generating (default 3)
```

Invariants:
- If `is_binary` is `True`, `hunks` is empty and `total_additions == total_deletions == 0`.
- If `original == ""` and `modified != ""`, the file is new (additions only).
- If `original != ""` and `modified == ""`, the file was deleted (deletions only).
- `size_bytes` is `len(modified.encode("utf-8"))` if not binary; else the raw byte
  count of the file.
- `total_additions == sum(h.additions for h in hunks)`.
- `total_deletions == sum(h.deletions for h in hunks)`.

### 1.6 Complete Module: `src/agenthicc/tui/diff_model.py`

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DiffLineType(Enum):
    CONTEXT = "context"
    ADDED = "added"
    REMOVED = "removed"
    HEADER = "header"
    FILE_HEADER = "file_header"


@dataclass(frozen=True)
class DiffLine:
    content: str
    line_type: DiffLineType
    line_number_old: int | None
    line_number_new: int | None


@dataclass(frozen=True)
class DiffHunk:
    header: str
    start_line_old: int
    start_line_new: int
    count_old: int
    count_new: int
    lines: tuple[DiffLine, ...]
    additions: int
    deletions: int


@dataclass(frozen=True)
class DiffResult:
    tool_use_id: str
    file_path: str
    original: str
    modified: str
    hunks: tuple[DiffHunk, ...]
    total_additions: int
    total_deletions: int
    is_binary: bool
    size_bytes: int
    context_lines: int = 3
```

---

## 2. Diff Generation

### 2.1 Module Location

```
src/agenthicc/tui/diff_engine.py
```

### 2.2 File Snapshot Store

`FileSnapshotStore` replaces the ad-hoc `_file_snapshots: dict` in `__main__.py`.
It must be instantiated once per agent turn and discarded afterward. It is NOT
thread-safe (single asyncio event loop only).

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Final


MAX_SNAPSHOT_FILE_BYTES: Final[int] = 1_048_576  # 1 MB


@dataclass
class _SnapshotEntry:
    file_path: str       # relative path as passed by the tool
    original: str        # content before the tool call; "" for new files
    abs_path: str        # absolute path for re-reading after completion


class FileSnapshotStore:
    """
    Captures before/after file content around write_file and patch_file
    tool calls.  One instance per agent turn; discard after the turn ends.

    Usage:
        store = FileSnapshotStore()

        # On ToolCallStarted signal:
        await store.capture_before(tool_use_id, file_path, cwd)

        # On ToolCallComplete signal:
        result = await store.capture_after_and_diff(tool_use_id)
        # result is DiffResult | None
    """

    # Tool names that warrant snapshot + diff.
    WATCHED_TOOLS: frozenset[str] = frozenset({"write_file", "patch_file"})

    def __init__(self) -> None:
        self._entries: dict[str, _SnapshotEntry] = {}

    async def capture_before(
        self,
        tool_use_id: str,
        file_path: str,
        cwd: str,
    ) -> None:
        """
        Read the file at file_path before the tool modifies it.
        Silently skips if file_path is empty or read fails.
        Must be called from an async context (uses asyncio.to_thread for I/O).
        """
        ...

    async def capture_after_and_diff(
        self,
        tool_use_id: str,
    ) -> DiffResult | None:
        """
        Read the file after the tool has completed.
        Compute and return DiffResult, or None if tool_use_id was not captured.
        Always removes the entry from the store (memory release).
        """
        ...

    def release(self, tool_use_id: str) -> None:
        """Explicitly release a snapshot without computing a diff."""
        self._entries.pop(tool_use_id, None)

    def clear(self) -> None:
        """Release all snapshots. Call at end of agent turn."""
        self._entries.clear()
```

#### 2.2.1 Capture-before Logic (detailed)

`capture_before` must:

1. Compute `abs_path`:
   - If `file_path` is absolute, use it directly.
   - Otherwise, `os.path.join(cwd, file_path)`.
2. If `os.path.exists(abs_path)`:
   a. Read the file with `asyncio.to_thread(lambda: open(abs_path, "rb").read())`.
   b. If `len(raw) > MAX_SNAPSHOT_FILE_BYTES`: store `original=""` and set a
      `_oversized: bool = True` flag in the entry. Do NOT read the content.
   c. Otherwise: decode as `raw.decode("utf-8", errors="replace")` and store.
3. If file does not exist: store `original=""` (new file scenario).
4. Store the `_SnapshotEntry` keyed by `tool_use_id`.
5. On ANY exception: silently return without storing (diff will be skipped).

#### 2.2.2 Capture-after-and-diff Logic (detailed)

`capture_after_and_diff` must:

1. Pop the `_SnapshotEntry` for `tool_use_id`. If not found, return `None`.
2. Read the new file content:
   a. `raw = await asyncio.to_thread(lambda: open(abs_path, "rb").read())` if
      `os.path.exists(abs_path)`, else `b""` (file was deleted).
   b. If `len(raw) > MAX_SNAPSHOT_FILE_BYTES` OR the entry was marked `_oversized`:
      Return a `DiffResult` with `is_binary=False`, `hunks=()`,
      `total_additions=0`, `total_deletions=0`, `size_bytes=len(raw)`, and a
      sentinel that `DiffRenderer` will detect as "too large to diff" (the
      `hunks` tuple being empty with `size_bytes > MAX_SNAPSHOT_FILE_BYTES`).
   c. Attempt `modified = raw.decode("utf-8")`. On `UnicodeDecodeError`:
      Return a `DiffResult` with `is_binary=True`, `hunks=()`,
      `total_additions=0`, `total_deletions=0`, `size_bytes=len(raw)`.
3. Call `compute_diff(tool_use_id, file_path, original, modified)` and return result.
4. On ANY exception: return `None`.

### 2.3 Diff Computation

```python
from __future__ import annotations

import difflib
import re
from typing import Final

from agenthicc.tui.diff_model import (
    DiffHunk,
    DiffLine,
    DiffLineType,
    DiffResult,
)


DEFAULT_CONTEXT_LINES: Final[int] = 3
MAX_HUNKS: Final[int] = 100


def compute_diff(
    tool_use_id: str,
    file_path: str,
    original: str,
    modified: str,
    context_lines: int = DEFAULT_CONTEXT_LINES,
) -> DiffResult:
    """
    Compute a DiffResult from two file contents.

    - Uses difflib.unified_diff with lineterm="" so every line is clean.
    - Parses the raw unified diff output into DiffHunk / DiffLine structures.
    - Never raises; returns a DiffResult with is_binary=False and empty hunks
      on malformed input.
    """
    ...
```

#### 2.3.1 Unified Diff Generation Rules

Always call `difflib.unified_diff` as:

```python
raw_lines = list(difflib.unified_diff(
    original.splitlines(),
    modified.splitlines(),
    fromfile=f"a/{file_path}",
    tofile=f"b/{file_path}",
    n=context_lines,
    lineterm="",       # REQUIRED: prevents double-newline artifacts
))
```

`lineterm=""` is mandatory. The existing code in `__main__.py` documents why:
`keepends=True` with `lineterm=""` concatenates `---`/`+++`/`@@` control lines
into a single giant line. Using `lineterm=""` with `splitlines()` avoids this.

#### 2.3.2 Hunk Parsing

Parse `raw_lines` into `DiffHunk` objects:

1. Lines starting with `"--- "` or `"+++ "` have `line_type = FILE_HEADER`.
   Assign `line_number_old=None`, `line_number_new=None`.
2. Lines starting with `"@@ "` start a new hunk:
   a. Parse the `@@ -old_start[,old_count] +new_start[,new_count] @@` header
      using the regex: `r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@'`.
   b. `old_count` defaults to `1` if the `,count` group is absent.
   c. `new_count` defaults to `1` if the `,count` group is absent.
   d. The header DiffLine has `line_type = HEADER`, both line numbers `None`.
3. Lines starting with `"+"` (but NOT `"+++ "`) have `line_type = ADDED`.
   Assign `line_number_old=None`, `line_number_new=<current_new>`. Increment
   `current_new`.
4. Lines starting with `"-"` (but NOT `"--- "`) have `line_type = REMOVED`.
   Assign `line_number_old=<current_old>`, `line_number_new=None`. Increment
   `current_old`.
5. All other lines (context, i.e. starting with `" "`) have `line_type = CONTEXT`.
   Assign both `line_number_old=<current_old>`, `line_number_new=<current_new>`.
   Increment both.
6. At end of each hunk (next `@@` line or end of input): finalize the `DiffHunk`
   with the collected lines, compute `additions` and `deletions` counts.

#### 2.3.3 New File and Deleted File Handling

- **New file** (`original == ""`): All diff lines will be `ADDED`. `total_deletions == 0`.
  The `fromfile` will show `a/{file_path}` but the diff will start at line 1 of the
  new file. This is standard `difflib.unified_diff` behavior.
- **Deleted file** (`modified == ""`): All diff lines will be `REMOVED`.
  `total_additions == 0`. The file content comes from `original`.
- **No changes** (`original == modified`): `difflib.unified_diff` returns an empty
  iterator. Return a `DiffResult` with `hunks=()`, `total_additions=0`,
  `total_deletions=0`, `is_binary=False`.

#### 2.3.4 Large Diff Handling

After parsing, if `len(hunks) > MAX_HUNKS` (100):
- Keep only the first 5 hunks.
- The `DiffResult.hunks` tuple contains only those 5 hunks.
- The renderer uses `len(hunks) < total_hunk_count` to detect truncation. To
  communicate the original count, store it in a separate field:

```python
@dataclass(frozen=True)
class DiffResult:
    ...
    total_hunk_count: int = 0  # actual hunk count before truncation; equals len(hunks) when not truncated
```

`total_hunk_count` must always reflect the count BEFORE truncation.
`len(hunks)` reflects what is stored.

#### 2.3.5 Binary File Handling

Binary detection happens in `capture_after_and_diff` (Section 2.2.2). The
`compute_diff` function only receives text; it does not detect binary files.
If called with non-UTF-8 content that somehow passed detection, it will work
normally on the decoded-with-errors text — the diff output may look noisy but
will not crash.

### 2.4 Performance Requirements

| Operation | Limit | Enforcement |
|---|---|---|
| Max file size for diff | 1 MB | `MAX_SNAPSHOT_FILE_BYTES = 1_048_576` checked in `capture_before` |
| Diff computation timing | Must complete in < 100ms for typical files | No hard enforcement; document in docstring |
| Lazy computation | Diff computed in `capture_after_and_diff`, NOT during `capture_before` | Architecture enforces this |
| Memory release | `_SnapshotEntry` popped from dict on `capture_after_and_diff` | `pop()` call in implementation |
| Turn-end cleanup | `store.clear()` called in `finally` block of agent turn | Caller responsibility (document in `FileSnapshotStore` docstring) |
| `DiffResult` immutability | All fields frozen | `@dataclass(frozen=True)` |

---

## 3. Inline Diff Rendering

### 3.1 Module Location

```
src/agenthicc/tui/diff_renderer.py
```

### 3.2 Renderer Class

```python
from __future__ import annotations

from typing import Final

from rich.console import Console
from rich.text import Text

from agenthicc.tui.diff_model import DiffHunk, DiffLine, DiffLineType, DiffResult


COMPACT_MAX_LINES: Final[int] = 20
COMPACT_CONTEXT_LINES: Final[int] = 2
MAX_FILE_SIZE_FOR_DIFF: Final[int] = 1_048_576   # 1 MB; must equal diff_engine.MAX_SNAPSHOT_FILE_BYTES
MAX_HUNKS_DISPLAY: Final[int] = 100
LARGE_DIFF_HUNK_PREVIEW: Final[int] = 5


class DiffRenderer:
    """
    Converts a DiffResult into a list of ANSI-formatted strings for
    committed-transcript output.

    All output is via Rich Text objects rendered to a Console with
    force_terminal=True, highlight=False, markup=False.

    Two public methods:
      render_compact(result)  → list[str]   (default, ≤20 diff lines)
      render_expanded(result) → list[str]   (full diff, all hunks)
    """

    def __init__(
        self,
        console: Console | None = None,
        terminal_width: int = 80,
        show_line_numbers: bool = False,
        no_color: bool = False,
    ) -> None:
        ...

    def render_compact(self, result: DiffResult) -> list[str]:
        """
        Render a compact view of the diff.

        Rules:
        1. If is_binary: return stats-only line (see Section 3.4).
        2. If size_bytes > MAX_FILE_SIZE_FOR_DIFF: return size-only stats line.
        3. If no changes (hunks empty, not binary, not oversized): return
           single line: "  (no changes)".
        4. Otherwise: render up to COMPACT_MAX_LINES diff lines across hunks,
           using COMPACT_CONTEXT_LINES context lines per change.
        5. If truncated: append truncation footer (see Section 3.1.2).
        Returns list of ANSI-formatted strings, one per terminal line.
        """
        ...

    def render_expanded(self, result: DiffResult) -> list[str]:
        """
        Render the full diff with all hunks and line numbers.

        Rules:
        1. Same binary/oversized/no-change handling as render_compact.
        2. Render ALL hunks (up to LARGE_DIFF_HUNK_PREVIEW if total_hunk_count > MAX_HUNKS_DISPLAY).
        3. Always show line numbers (overrides show_line_numbers constructor arg).
        4. If total_hunk_count > MAX_HUNKS_DISPLAY: append large-diff summary footer.
        Returns list of ANSI-formatted strings, one per terminal line.
        """
        ...

    def render_stats_only(self, result: DiffResult) -> list[str]:
        """
        Render a single-line stats summary.
        Used when file is too large or binary.
        Format: "  {file_path}  +{additions} additions, -{deletions} deletions"
        or "  {file_path}  binary file, {size_bytes} bytes"
        """
        ...
```

### 3.3 Compact View Specification (Section 3.1)

#### 3.3.1 Structure

The compact view consists of:

1. **File header line** — `─── {file_path} ` padded to terminal width with `─`
2. **Diff lines** — up to `COMPACT_MAX_LINES` (20) lines total
3. **Truncation footer** (if truncated) — see Section 3.3.2
4. **Footer separator** — `─` × terminal_width

#### 3.3.2 Truncation Footer

When the diff has more lines than `COMPACT_MAX_LINES`:

```
  … +{total_additions} additions, -{total_deletions} deletions  /expand {short_id}
```

Where `short_id = tool_use_id[:8]` (first 8 characters of the UUID).

This line is dim-styled (`\x1b[2m`) except for `/expand {short_id}` which is
rendered in cyan (`\x1b[36m`) to indicate it is an interactive command.

#### 3.3.3 Line Selection for Compact View

When the full diff exceeds `COMPACT_MAX_LINES` lines:

1. Iterate hunks in order.
2. For each hunk, include up to `COMPACT_CONTEXT_LINES` (2) context lines before
   and after each changed block within the hunk.
3. Stop when the running total reaches `COMPACT_MAX_LINES`.
4. If mid-hunk, add a `… ({remaining} more lines in this hunk)` dim line.

### 3.4 Expanded View Specification (Section 3.2)

The expanded view renders all hunks with line numbers. Line number column width
is `len(str(max(start_line_old + count_old, start_line_new + count_new)))` across
all hunks, so all line numbers align in a fixed column.

Format per line (with line numbers enabled):
```
{old_num:>4} {new_num:>4} {prefix}{content}
```

Where:
- `old_num` is dim-styled; blank for ADDED lines.
- `new_num` is dim-styled; blank for REMOVED lines.
- Both are blank for HEADER lines.
- `prefix` is `+`, `-`, or ` `.

### 3.5 Color Specification

All colors are applied via Rich `Text.append(text, style=...)`. Do NOT use raw
ANSI escape sequences in rendering code — use Rich style names.

| Line type | Rich style | ANSI equivalent | Example |
|---|---|---|---|
| ADDED lines | `"green"` | `\x1b[32m` | `+    return await verify(token)` |
| REMOVED lines | `"red"` | `\x1b[31m` | `-    return verify(token)` |
| CONTEXT lines | `"dim"` | `\x1b[2m` | `     try:` |
| HEADER lines (@@ ... @@) | `"cyan dim"` | `\x1b[36m\x1b[2m` | `@@ -85,7 +85,7 @@` |
| FILE_HEADER lines (--- / +++) | `"bold"` | `\x1b[1m` | `--- a/src/auth.py` |
| File header separator | `"dim"` | `\x1b[2m` | `─── src/auth.py ────` |
| Truncation text | `"dim"` | `\x1b[2m` | `… +3 additions ...` |
| /expand command | `"cyan"` | `\x1b[36m` | `/expand abc12345` |
| Line numbers | `"dim"` | `\x1b[2m` | `  85   87 ` |
| Stats line (binary/large) | `"dim"` | `\x1b[2m` | `  binary file, 2048 bytes` |

**NO_COLOR mode**: When `no_color=True` is passed to `DiffRenderer.__init__`:
- Construct `Console` with `no_color=True`.
- All Rich style names produce no ANSI output.
- `+` and `-` prefixes on ADDED/REMOVED lines remain as the sole visual differentiator.

### 3.6 Large Diff Handling

| Condition | Rendering |
|---|---|
| `result.size_bytes > MAX_FILE_SIZE_FOR_DIFF` | Stats-only line: file path + "+N additions -M deletions" with "(diff too large)" note |
| `result.is_binary == True` | Stats-only line: "binary file, N bytes" |
| `result.total_hunk_count > MAX_HUNKS_DISPLAY (100)` | Show first `LARGE_DIFF_HUNK_PREVIEW` (5) hunks + footer: `(… {remaining} more hunks, {total_additions - shown_additions} more additions)` |
| `hunks == ()` and not binary and `size_bytes <= MAX_FILE_SIZE_FOR_DIFF` | Single line: `  (no changes)` |

### 3.7 Rendering to Committed Transcript

`DiffRenderer` produces `list[str]` where each string is one terminal line with
embedded ANSI escape sequences. The caller passes this list to
`terminal.commit_lines(lines)` (or the equivalent transcript method).

The renderer must:
1. Instantiate `Console(file=io.StringIO(), force_terminal=True, highlight=False, width=terminal_width, no_color=no_color)`.
2. For each logical line, create a `rich.text.Text` object, `.append()` the parts
   with styles, then print to the console.
3. Extract the string from `console.file.getvalue()`.
4. Split by `\n`, strip trailing empty strings, return the list.

Each call to `render_compact` or `render_expanded` must use a fresh `StringIO`
buffer — the `Console` should be recreated or the file reset between calls.

---

## 4. Approval Integration

### 4.1 Diff Before Approval

The approval gate (Section 7.3 of the master PRD) requires: **the diff is
committed to the transcript BEFORE the approval gate appears in the bottom block**.

The integration sequence for `write_file` in `REVIEW`/`ASK`/`SAFE` mode:

```
1. ToolCallStarted signal fires
   → FileSnapshotStore.capture_before(tool_use_id, path, cwd)

2. ApprovalGate blocks execution (tool has NOT run yet)
   → Agent requests write; tool executor pauses for approval
   → DiffResult CANNOT be computed yet (file not yet written)
   → Show "proposed write" diff using the agent's intended content

3. Approval context diff rendering:
   → The tool args include the new file content (for write_file) or patch
   → Compute a "preview diff" from original_snapshot vs proposed_content
   → Commit to transcript:
        ─── Proposed change: src/auth/session.py ──────────────
        @@ -145,6 +145,6 @@
        - expiry = datetime.now() + ...
        + expiry = datetime.now(timezone.utc) + ...
        ────────────────────────────────────────────────────────

4. Approval gate appears in bottom block:
        ⚠ write_file(path='src/auth/session.py') — approve?
        [Y] Allow    [N] Deny    [A] Allow all this session

5a. User approves → tool runs → ToolCallComplete fires
    → FileSnapshotStore.capture_after_and_diff()
    → If actual diff differs from preview diff, commit actual diff
    → Otherwise skip (avoid duplicate diff in transcript)

5b. User denies → release snapshot from store
    → commit: "  ⎿ write_file(...)  ✗ denied by user"
```

### 4.2 Preview Diff Function

```python
from __future__ import annotations

from agenthicc.tui.diff_engine import compute_diff
from agenthicc.tui.diff_model import DiffResult


def compute_preview_diff(
    tool_use_id: str,
    file_path: str,
    original_content: str,
    proposed_content: str,
) -> DiffResult:
    """
    Compute a "preview diff" showing what the file would look like if the
    proposed write were approved.  Uses the same compute_diff function.
    The tool_use_id is prefixed with "preview:" to distinguish preview diffs
    from actual post-execution diffs.
    """
    return compute_diff(
        tool_use_id=f"preview:{tool_use_id}",
        file_path=file_path,
        original=original_content,
        modified=proposed_content,
    )
```

### 4.3 Diff Deduplication

After a write is approved and executed, `capture_after_and_diff` produces the
actual diff. Before committing it:

1. Compare `actual_diff.total_additions` and `actual_diff.total_deletions` with
   the preview diff values.
2. If both match AND `actual_diff.hunks == preview_diff.hunks`: skip committing
   the actual diff (already shown).
3. If they differ (e.g., a patch_file added more than shown): commit the actual
   diff with a header: `─── Actual change (differs from preview): {file_path} ───`

The comparison of `hunks` tuples is by value equality (frozen dataclasses support
`__eq__` by default).

---

## 5. Diff Storage

### 5.1 In-Memory Storage Policy

`DiffResult` objects are stored in memory only for the current session. There is
no disk persistence — diffs can always be regenerated from `original` and `modified`
strings if needed, but those strings themselves are also only in-memory.

### 5.2 `DiffStore` Class

```python
from __future__ import annotations

from collections import OrderedDict
from typing import Final

from agenthicc.tui.diff_model import DiffResult


MAX_STORED_DIFFS: Final[int] = 50   # evict oldest when exceeded


class DiffStore:
    """
    In-memory LRU store for DiffResult objects.  Keyed by tool_use_id.
    Evicts oldest entries when MAX_STORED_DIFFS is exceeded.

    Thread-safety: single asyncio event loop only; no locking.
    """

    def __init__(self, max_size: int = MAX_STORED_DIFFS) -> None:
        self._store: OrderedDict[str, DiffResult] = OrderedDict()
        self._max_size = max_size

    def put(self, result: DiffResult) -> None:
        """Store a DiffResult. Evicts oldest if at capacity."""
        if result.tool_use_id in self._store:
            self._store.move_to_end(result.tool_use_id)
        else:
            if len(self._store) >= self._max_size:
                self._store.popitem(last=False)  # evict oldest
            self._store[result.tool_use_id] = result

    def get(self, tool_use_id: str) -> DiffResult | None:
        """Retrieve a stored diff. Returns None if not found."""
        return self._store.get(tool_use_id)

    def get_all(self) -> list[DiffResult]:
        """Return all stored diffs, oldest first."""
        return list(self._store.values())

    def clear(self) -> None:
        """Remove all stored diffs (e.g., on session end)."""
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)
```

### 5.3 Memory Eviction Rules

- `DiffStore` enforces `MAX_STORED_DIFFS = 50`. When the 51st diff is added, the
  oldest is evicted (LRU order via `OrderedDict`).
- `FileSnapshotStore` entries are popped on `capture_after_and_diff` or `release`.
  `clear()` is called in the agent turn `finally` block.
- `DiffResult.original` and `DiffResult.modified` are stored in-memory for the
  session. For 1 MB files at 50 stored diffs, worst-case RSS contribution is
  ~100 MB. In practice files are smaller. For files exceeding 256 KB, consider
  not storing `original`/`modified` strings and instead storing empty strings,
  relying on the pre-computed `hunks` only.

### 5.4 No Disk Persistence

Diffs are NOT written to `events.jsonl`, `~/.agenthicc/`, or any other file.
The justification: diffs are derived data; they can be regenerated from the
original and modified content if the session is replayed. Storing large diff
strings in `events.jsonl` would bloat the session log and provide no new
information beyond what the tool call arguments already record.

---

## 6. `/expand` Command Integration

### 6.1 Command Registration

Register an `/expand` slash command in the command registry:

```python
# src/agenthicc/commands/builtins.py  (add to existing file)

@register_command("expand")
async def cmd_expand(
    args: str,
    context: CommandContext,
) -> None:
    """
    /expand <tool_use_id_prefix>

    Expand a previously-shown compact diff to show all hunks and line numbers.
    The tool_use_id_prefix is the first 8 characters shown in the truncation footer.
    """
    prefix = args.strip()
    if not prefix:
        context.transcript.commit_error("Usage: /expand <id>  (8-char prefix from diff footer)")
        return

    # Find matching diff in DiffStore
    diff_store: DiffStore = context.diff_store
    matches = [
        r for r in diff_store.get_all()
        if r.tool_use_id.startswith(prefix)
    ]
    if not matches:
        context.transcript.commit_error(f"No diff found with ID prefix: {prefix}")
        return
    if len(matches) > 1:
        context.transcript.commit_error(
            f"Ambiguous prefix '{prefix}' matches {len(matches)} diffs. Use more characters."
        )
        return

    result = matches[0]
    renderer = DiffRenderer(
        terminal_width=context.terminal.size.cols,
        show_line_numbers=True,
        no_color=context.terminal.capabilities.no_color,
    )
    lines = renderer.render_expanded(result)
    context.transcript.commit_lines(lines)
```

### 6.2 CommandContext Extension

`CommandContext` (in `src/agenthicc/commands/command.py`) must gain a `diff_store`
attribute:

```python
@dataclass
class CommandContext:
    model: Any          # TranscriptModel
    terminal: Any       # Terminal
    diff_store: DiffStore  # add this field
    ...
```

---

## 7. Module Structure Summary

```
src/agenthicc/tui/
    diff_model.py       # DiffLineType, DiffLine, DiffHunk, DiffResult
    diff_engine.py      # FileSnapshotStore, compute_diff, compute_preview_diff,
                        # MAX_SNAPSHOT_FILE_BYTES, DEFAULT_CONTEXT_LINES, MAX_HUNKS
    diff_renderer.py    # DiffRenderer, DiffStore
```

Integration points:
- `src/agenthicc/__main__.py` — replace `_file_snapshots` dict with
  `FileSnapshotStore`; call `DiffStore.put()` after each diff; pass `diff_store`
  to `CommandContext`.
- `src/agenthicc/commands/builtins.py` — add `/expand` command.
- `src/agenthicc/commands/command.py` — add `diff_store: DiffStore` to
  `CommandContext`.

---

## 8. Test Specification

### 8.1 Unit Tests: `tests/unit/test_diff_model.py`

**Test file:** `tests/unit/test_diff_model.py`

```python
import pytest
from agenthicc.tui.diff_model import DiffLine, DiffLineType, DiffHunk, DiffResult

@pytest.mark.unit
class TestDiffLine:
    ...

@pytest.mark.unit
class TestDiffHunk:
    ...

@pytest.mark.unit
class TestDiffResult:
    ...
```

Enumerate 15 unit tests for the data model:

| # | Test name | Inputs | Expected output | Edge case |
|---|---|---|---|---|
| DM-01 | `test_diffline_frozen` | Create `DiffLine`, attempt `dl.content = "x"` | `FrozenInstanceError` raised | Immutability |
| DM-02 | `test_diffline_added_no_old_line_number` | `DiffLine("+foo", DiffLineType.ADDED, None, 5)` | `line_number_old is None`, `line_number_new == 5` | ADDED invariant |
| DM-03 | `test_diffline_removed_no_new_line_number` | `DiffLine("-foo", DiffLineType.REMOVED, 3, None)` | `line_number_old == 3`, `line_number_new is None` | REMOVED invariant |
| DM-04 | `test_diffline_context_both_line_numbers` | `DiffLine(" foo", DiffLineType.CONTEXT, 3, 5)` | Both line numbers set | CONTEXT invariant |
| DM-05 | `test_diffhunk_frozen` | Create `DiffHunk`, attempt mutation | `FrozenInstanceError` raised | Immutability |
| DM-06 | `test_diffhunk_additions_deletions_counts` | Hunk with 3 ADDED, 2 REMOVED lines | `additions == 3`, `deletions == 2` | Count accuracy |
| DM-07 | `test_diffhunk_empty_lines` | `DiffHunk(..., lines=(), additions=0, deletions=0)` | `len(lines) == 0` | Empty hunk |
| DM-08 | `test_diffresult_frozen` | Create `DiffResult`, attempt mutation | `FrozenInstanceError` raised | Immutability |
| DM-09 | `test_diffresult_binary_empty_hunks` | `DiffResult(is_binary=True, hunks=(), ...)` | `len(hunks) == 0`, `total_additions == 0` | Binary invariant |
| DM-10 | `test_diffresult_total_counts_match_hunks` | Two hunks with 3+2 additions and 1+4 deletions | `total_additions == 5`, `total_deletions == 5` | Sum correctness |
| DM-11 | `test_diffresult_new_file_zero_deletions` | `original=""`, `modified="hello\n"` | `total_deletions == 0` when computed | New file |
| DM-12 | `test_diffresult_deleted_file_zero_additions` | `original="hello\n"`, `modified=""` | `total_additions == 0` when computed | Deleted file |
| DM-13 | `test_diffresult_no_changes` | `original == modified` | `hunks == ()` | Identical content |
| DM-14 | `test_diffline_type_values` | Access all `DiffLineType` members | All 5 members exist with correct string values | Enum completeness |
| DM-15 | `test_diffresult_context_lines_default` | Construct `DiffResult` without `context_lines` | `context_lines == 3` | Default value |

### 8.2 Unit Tests: `tests/unit/test_diff_engine.py`

**Test file:** `tests/unit/test_diff_engine.py`

Enumerate 30 unit tests for `compute_diff` and `FileSnapshotStore`:

| # | Test name | Inputs | Expected output | Edge case |
|---|---|---|---|---|
| DE-01 | `test_compute_diff_simple_change` | `original="foo\n"`, `modified="bar\n"` | 1 hunk, 1 REMOVED, 1 ADDED | Basic change |
| DE-02 | `test_compute_diff_no_change` | `original == modified == "hello\n"` | `hunks == ()`, both totals 0 | Identical |
| DE-03 | `test_compute_diff_new_file` | `original=""`, `modified="line1\nline2\n"` | All ADDED, no REMOVED | New file |
| DE-04 | `test_compute_diff_deleted_file` | `original="line1\nline2\n"`, `modified=""` | All REMOVED, no ADDED | Deleted file |
| DE-05 | `test_compute_diff_multiline_change` | 5-line original, 3 lines changed | Correct hunk counts | Multi-line |
| DE-06 | `test_compute_diff_context_lines_respected` | Change at line 10, `context_lines=2` | At most 2 context lines before/after | Context param |
| DE-07 | `test_compute_diff_hunk_header_parsed` | Change at line 85 of 100-line file | `hunk.start_line_old == 83` (with 2 context) | Header parsing |
| DE-08 | `test_compute_diff_line_numbers_monotonic` | Any change | `line_number_old` increments for REMOVED/CONTEXT; `line_number_new` for ADDED/CONTEXT | Line number monotonicity |
| DE-09 | `test_compute_diff_multiple_hunks` | Changes at lines 5 and 50 (far apart) | Two hunks produced | Multi-hunk |
| DE-10 | `test_compute_diff_hunk_additions_deletions` | Change: 2 removed, 3 added | `hunk.additions == 3`, `hunk.deletions == 2` | Hunk counts |
| DE-11 | `test_compute_diff_total_counts` | Multiple hunks with known changes | `result.total_additions == sum(h.additions)` | Total accuracy |
| DE-12 | `test_compute_diff_file_header_lines` | Any change | First two lines are `FILE_HEADER` type | Header lines |
| DE-13 | `test_compute_diff_lineterm_empty` | Change with trailing newline in original | No double newlines in diff output | `lineterm=""` |
| DE-14 | `test_compute_diff_fromfile_tofile` | `file_path="src/foo.py"` | FILE_HEADER contains `"a/src/foo.py"` and `"b/src/foo.py"` | fromfile/tofile |
| DE-15 | `test_compute_diff_large_hunk_count_truncated` | Synthetic file with 150 distinct changes | `len(result.hunks) <= LARGE_DIFF_HUNK_PREVIEW`, `result.total_hunk_count > MAX_HUNKS` | Truncation |
| DE-16 | `test_compute_diff_context_line_type` | Context line in a hunk | `DiffLineType.CONTEXT` | Context type |
| DE-17 | `test_compute_diff_header_line_type` | `@@` line | `DiffLineType.HEADER`, both line numbers `None` | Header type |
| DE-18 | `test_compute_diff_added_line_content_prefix` | Added line `"newcode"` | `content == "+newcode"` | Content prefix |
| DE-19 | `test_compute_diff_removed_line_content_prefix` | Removed line `"oldcode"` | `content == "-oldcode"` | Content prefix |
| DE-20 | `test_compute_diff_context_line_content_prefix` | Context line `"unchanged"` | `content == " unchanged"` | Content prefix |
| DE-21 | `test_file_snapshot_store_capture_new_file` | File does not exist at capture time | `original == ""` | New file |
| DE-22 | `test_file_snapshot_store_capture_existing` | File exists with known content | `original == known_content` | Existing file |
| DE-23 | `test_file_snapshot_store_capture_after_produces_diff` | Write new content then `capture_after_and_diff` | `DiffResult` with correct additions | Full round-trip |
| DE-24 | `test_file_snapshot_store_unknown_id_returns_none` | `capture_after_and_diff("unknown_id")` | Returns `None` | Unknown ID |
| DE-25 | `test_file_snapshot_store_release_removes_entry` | Capture then `release(id)` | `get` returns `None`; `capture_after_and_diff` returns `None` | Release |
| DE-26 | `test_file_snapshot_store_clear_empties_store` | Capture 3 files then `clear()` | All `capture_after_and_diff` return `None` | Clear |
| DE-27 | `test_file_snapshot_store_oversized_returns_stats_diff` | File size > 1MB | `DiffResult` with `hunks == ()`, `size_bytes > MAX_SNAPSHOT_FILE_BYTES` | Oversized |
| DE-28 | `test_file_snapshot_store_binary_file_returns_binary_diff` | Binary file (non-UTF-8 bytes) | `DiffResult.is_binary == True` | Binary |
| DE-29 | `test_compute_diff_empty_original_empty_modified` | Both empty | `hunks == ()`, `total_additions == 0`, `total_deletions == 0` | Empty-empty |
| DE-30 | `test_compute_diff_single_line_no_newline` | `original="foo"`, `modified="bar"` | 1 hunk, correct diff | No trailing newline |

### 8.3 Unit Tests: `tests/unit/test_diff_renderer.py`

Enumerate 20 unit tests for `DiffRenderer` and `DiffStore`:

| # | Test name | Inputs | Expected output | Edge case |
|---|---|---|---|---|
| DR-01 | `test_render_compact_returns_list_of_strings` | Simple 1-line change | `list[str]` | Return type |
| DR-02 | `test_render_compact_file_header_present` | `file_path="src/foo.py"` | First line contains `"src/foo.py"` | File header |
| DR-03 | `test_render_compact_added_line_green` | 1 ADDED line | Output contains `\x1b[32m` (green) | Color ADDED |
| DR-04 | `test_render_compact_removed_line_red` | 1 REMOVED line | Output contains `\x1b[31m` (red) | Color REMOVED |
| DR-05 | `test_render_compact_hunk_header_cyan` | Hunk with `@@` line | Output contains `\x1b[36m` | Color HEADER |
| DR-06 | `test_render_compact_truncation_footer_present` | Diff with 50+ lines | Last line before separator contains `"/expand"` | Truncation |
| DR-07 | `test_render_compact_truncation_footer_id` | `tool_use_id="abcdef1234567890"` | Footer contains `"abcdef12"` (first 8 chars) | Short ID |
| DR-08 | `test_render_compact_no_truncation_when_small` | Diff with 5 lines | No truncation footer | No truncation |
| DR-09 | `test_render_expanded_shows_all_hunks` | Diff with 3 hunks | All 3 hunk headers present in output | Full expansion |
| DR-10 | `test_render_expanded_line_numbers` | Change at line 85 | Output contains `"85"` | Line numbers |
| DR-11 | `test_render_binary_file_stats_only` | `is_binary=True` | Single stats line, no `+`/`-` content | Binary |
| DR-12 | `test_render_oversized_file_stats_only` | `size_bytes > MAX_FILE_SIZE_FOR_DIFF` | Single stats line with byte count | Oversized |
| DR-13 | `test_render_no_changes` | `hunks == ()`, not binary, small file | `["  (no changes)"]` or similar | No change |
| DR-14 | `test_render_no_color_mode` | `no_color=True` | No `\x1b[` sequences in output | NO_COLOR |
| DR-15 | `test_render_no_color_prefix_still_present` | `no_color=True`, ADDED line | `"+"` prefix still visible | NO_COLOR prefix |
| DR-16 | `test_diff_store_put_and_get` | `put(result)` then `get(tool_use_id)` | Returns same `DiffResult` | Basic store |
| DR-17 | `test_diff_store_max_size_eviction` | Put `MAX_STORED_DIFFS + 1` diffs | Oldest evicted | LRU eviction |
| DR-18 | `test_diff_store_unknown_id_returns_none` | `get("unknown")` | `None` | Missing key |
| DR-19 | `test_diff_store_clear` | Put 3, `clear()`, then `len()` | `len() == 0` | Clear |
| DR-20 | `test_render_compact_footer_separator_present` | Any diff | Last line is `─` characters | Footer separator |

### 8.4 Integration Tests: `tests/integration/test_diff_pipeline.py`

Enumerate 15 integration tests combining engine + renderer:

| # | Test name | Scenario | Expected |
|---|---|---|---|
| INT-01 | `test_full_pipeline_simple_write` | Write file, capture snapshot, compute diff, render compact | Rendered output contains green `+` line |
| INT-02 | `test_full_pipeline_patch_file` | Patch file with known change, render | Correct line counts in compact output |
| INT-03 | `test_full_pipeline_new_file_created` | File did not exist before write | All ADDED lines, no REMOVED |
| INT-04 | `test_full_pipeline_file_deleted` | File exists before, empty after | All REMOVED lines, no ADDED |
| INT-05 | `test_full_pipeline_binary_file` | Write binary content | `is_binary=True`, stats-only render |
| INT-06 | `test_full_pipeline_large_file_stats` | File > 1MB | Stats-only render, no hunks |
| INT-07 | `test_full_pipeline_multiline_change_renders_correctly` | 20-line change | Compact shows first 20 diff lines then truncation footer |
| INT-08 | `test_expand_command_renders_full_diff` | Compact shown, then `/expand` called | Expanded output has all hunks |
| INT-09 | `test_expand_command_unknown_id` | `/expand zzzzz` | Error message committed |
| INT-10 | `test_expand_command_ambiguous_prefix` | Two diffs share first 8 chars | Error message committed |
| INT-11 | `test_diff_store_eviction_on_overflow` | 51 diffs stored | 1st diff evicted, 51st present |
| INT-12 | `test_snapshot_store_cleared_after_turn` | `store.clear()` after turn | Second turn does not find old snapshots |
| INT-13 | `test_preview_diff_vs_actual_diff_match` | Preview matches actual | Deduplication skips second commit |
| INT-14 | `test_preview_diff_vs_actual_diff_mismatch` | Patch changes more than preview | Actual diff committed with mismatch header |
| INT-15 | `test_no_color_pipeline` | Full pipeline with `NO_COLOR=1` | All output strings contain no `\x1b[` |

### 8.5 E2E Tests: `tests/e2e/test_diff_viewer_e2e.py`

Enumerate 10 E2E tests verifying terminal output through the rendering pipeline:

| # | Test name | User scenario | Expected terminal display | Additional assertions |
|---|---|---|---|---|
| E2E-01 | `test_write_file_diff_appears_in_transcript` | Agent calls `write_file`; tool completes | Committed lines contain `+` (green) and `-` (red) diff lines above approval gate | `DiffStore` has 1 entry |
| E2E-02 | `test_compact_diff_has_truncation_footer` | Agent writes 100-line change to a file | Compact view shows ≤20 lines + truncation footer with `/expand` | Footer contains 8-char ID |
| E2E-03 | `test_expand_slash_command_shows_full_diff` | After compact shown, user types `/expand {id}` | Full diff committed below compact diff; all hunks present | Expanded output > compact output lines |
| E2E-04 | `test_binary_file_shows_stats_only` | Agent writes binary file | Single stats line committed: `binary file, N bytes` | No ANSI red/green lines |
| E2E-05 | `test_large_file_shows_stats_only` | Agent writes file > 1MB | Stats line committed | No diff hunks in output |
| E2E-06 | `test_no_color_env_disables_ansi` | `NO_COLOR=1` in env; agent writes file | Committed lines contain `+`/`-` prefixes but no `\x1b[` sequences | Plain text output |
| E2E-07 | `test_approval_shows_preview_diff_before_gate` | Review mode; agent proposes write | Preview diff committed BEFORE approval prompt appears | Scroll position: diff above gate |
| E2E-08 | `test_new_file_all_additions` | Agent creates new file | All lines green with `+` prefix; no red lines | `total_deletions == 0` |
| E2E-09 | `test_diff_committed_to_scrollback_not_bottom_block` | Agent writes file | Diff lines are in `terminal.committed_lines`, not in bottom block | `terminal.bottom_block` contains no diff |
| E2E-10 | `test_no_duplicate_diff_when_preview_matches_actual` | Preview matches actual after approval | Only one diff block committed, not two | Single header line for diff |

### 8.6 Test Infrastructure

#### FakeTerminal for Unit/Integration Tests

The existing test infrastructure must include (or already includes) a `FakeTerminal`
that captures `commit_lines` calls. Tests asserting on rendered output should use:

```python
from agenthicc.tui.terminal import FakeTerminal

def test_example():
    fake = FakeTerminal(cols=80, rows=24)
    renderer = DiffRenderer(terminal_width=fake.size.cols)
    result = compute_diff("test-id", "src/foo.py", "old\n", "new\n")
    lines = renderer.render_compact(result)
    fake.commit_lines(lines)
    # Assert on fake.committed_lines
    assert any("+" in line for line in fake.committed_lines)
```

#### Tmp File Fixtures

Integration tests that exercise `FileSnapshotStore` use `tmp_path` (pytest's
built-in `tmp_path` fixture) for file I/O:

```python
import asyncio
import pytest
from agenthicc.tui.diff_engine import FileSnapshotStore

@pytest.mark.integration
async def test_snapshot_round_trip(tmp_path):
    original_file = tmp_path / "auth.py"
    original_file.write_text("def foo():\n    return 1\n")

    store = FileSnapshotStore()
    await store.capture_before("tid-001", str(original_file), str(tmp_path))

    # Simulate the tool writing new content
    original_file.write_text("def foo():\n    return 2\n")

    result = await store.capture_after_and_diff("tid-001")
    assert result is not None
    assert result.total_additions == 1
    assert result.total_deletions == 1
```

---

## 9. Acceptance Criteria

All criteria are binary (pass/fail). There are no subjective criteria.

| # | Criterion | Verification Method |
|---|---|---|
| AC-01 | `DiffResult`, `DiffHunk`, `DiffLine` are all `frozen=True` dataclasses | `pytest.raises(FrozenInstanceError)` in DM-01, DM-05, DM-08 |
| AC-02 | `DiffLineType` has exactly 5 members: CONTEXT, ADDED, REMOVED, HEADER, FILE_HEADER | DM-14 |
| AC-03 | `compute_diff` uses `difflib.unified_diff` with `lineterm=""` | Inspect raw output; no `\n` at end of individual lines (DE-13) |
| AC-04 | `compute_diff` produces correct `fromfile`/`tofile` in FILE_HEADER lines | DE-14 |
| AC-05 | `FileSnapshotStore.WATCHED_TOOLS` contains exactly `write_file` and `patch_file` | Inspect constant |
| AC-06 | Files > 1 MB are not stored as full strings; `DiffResult.hunks` is empty for oversized files | DE-27 |
| AC-07 | Binary files (non-UTF-8) produce `DiffResult.is_binary == True` | DE-28, DR-11 |
| AC-08 | `DiffRenderer.render_compact` returns `list[str]` (not a generator, not None) | DR-01 |
| AC-09 | ADDED lines render with `\x1b[32m` (Rich green) | DR-03 |
| AC-10 | REMOVED lines render with `\x1b[31m` (Rich red) | DR-04 |
| AC-11 | HEADER lines render with `\x1b[36m` (Rich cyan) | DR-05 |
| AC-12 | Compact view shows at most 20 diff lines before truncation footer | DR-06, INT-07 |
| AC-13 | Truncation footer contains `/expand {first_8_chars_of_tool_use_id}` | DR-07 |
| AC-14 | `DiffRenderer(no_color=True)` produces no `\x1b[` sequences | DR-14 |
| AC-15 | `DiffRenderer(no_color=True)` still shows `+`/`-` prefixes | DR-15 |
| AC-16 | `DiffStore` evicts oldest entry when `MAX_STORED_DIFFS + 1` are stored | DR-17, INT-11 |
| AC-17 | `/expand` command retrieves diff by 8-char prefix and commits expanded output | INT-08, E2E-03 |
| AC-18 | Diff appears in `terminal.committed_lines`, NOT in bottom block | E2E-09 |
| AC-19 | Approval gate shows preview diff BEFORE the Y/N prompt | E2E-07 |
| AC-20 | `FileSnapshotStore.clear()` releases all entries | DE-26, INT-12 |
| AC-21 | Duplicate diff is NOT committed when preview matches actual | INT-13, E2E-10 |
| AC-22 | `compute_diff` handles both new-file (`original=""`) and deleted-file (`modified=""`) | DE-03, DE-04 |
| AC-23 | Hunk truncation at >100 hunks: `len(result.hunks) == LARGE_DIFF_HUNK_PREVIEW` and `result.total_hunk_count > 100` | DE-15 |
| AC-24 | All public functions and classes have complete type annotations passing `mypy --strict` | CI mypy run |
| AC-25 | Every source file begins with `from __future__ import annotations` | Code inspection |
| AC-26 | No raw ANSI escape sequences (`\x1b[`) in `diff_renderer.py` — only Rich style names | Code review |
| AC-27 | `compute_diff` with `original == modified` returns `DiffResult` with `hunks == ()` | DE-02 |
| AC-28 | Unit test coverage for `diff_model.py` is 100% | `pytest --cov=agenthicc.tui.diff_model` |
| AC-29 | Unit test coverage for `diff_engine.py` is >= 95% | `pytest --cov=agenthicc.tui.diff_engine` |
| AC-30 | Unit test coverage for `diff_renderer.py` is >= 90% | `pytest --cov=agenthicc.tui.diff_renderer` |
| AC-31 | `DiffResult.total_additions == sum(h.additions for h in result.hunks)` | DM-10, enforced in `compute_diff` |
| AC-32 | `DiffResult.total_deletions == sum(h.deletions for h in result.hunks)` | DM-10, enforced in `compute_diff` |
| AC-33 | Binary files show only stats line in compact render, no hunks or `+`/`-` lines | DR-11, E2E-04 |
| AC-34 | Oversized files show only stats line in compact render | DR-12, E2E-05 |
| AC-35 | `NO_COLOR=1` in environment disables all ANSI output end-to-end | INT-15, E2E-06 |

---

## 10. Dependencies

### 10.1 New Python Dependencies

No new third-party dependencies are required:
- `difflib` — stdlib, already available.
- `rich` — already in project dependencies (used in existing TUI code).
- `dataclasses` — stdlib.
- `asyncio` — stdlib.
- `collections.OrderedDict` — stdlib.

### 10.2 Internal Dependencies

| New module | Depends on |
|---|---|
| `diff_model.py` | No internal deps |
| `diff_engine.py` | `diff_model.py` |
| `diff_renderer.py` | `diff_model.py`, `rich` |
| `commands/builtins.py` (modified) | `diff_renderer.py`, `diff_model.py` |
| `commands/command.py` (modified) | `diff_renderer.py` |
| `__main__.py` (modified) | `diff_engine.py`, `diff_renderer.py` |

### 10.3 Breaking Changes

The existing ad-hoc implementation in `__main__.py` (lines 326–422) must be
replaced:

- Delete `_file_snapshots: dict[str, tuple[str, str]] = {}` and all `_file_snapshots`
  usages.
- Delete the inline `difflib.unified_diff` call (lines 412–418).
- Replace with `FileSnapshotStore` instantiated at top of `_run_agent_turn()` and
  cleared in the `finally` block.
- Replace `transcript.finish_tool_call(tool_use_id=tid, output=diff)` with
  a `DiffStore.put(result)` call followed by committing the rendered compact diff
  to the transcript via `terminal.commit_lines(renderer.render_compact(result))`.

The transcript `finish_tool_call` method signature does not change. The diff string
previously passed to it is replaced by the rendered lines committed to the terminal
directly, which is consistent with the committed-transcript architecture.

---

## 11. Example Rendered Output

### 11.1 Compact Diff (typical change)

```
─── src/auth/session.py ───────────────────────────────────────────────────────
@@ -145,6 +145,6 @@
       def validate_token(token: SessionToken) -> bool:
           """Check if the session token is still valid."""
-    expiry = datetime.now() + timedelta(hours=24)
+    expiry = datetime.now(timezone.utc) + timedelta(hours=24)
-    if token.expiry < datetime.now():
+    if token.expiry < datetime.now(timezone.utc):
           raise TokenExpiredError(f"Token {token.id} expired")
────────────────────────────────────────────────────────────────────────────────
```

ANSI rendering:
- `─── src/auth/session.py ───` : `\x1b[2m` (dim)
- `@@ -145,6 +145,6 @@` : `\x1b[36m\x1b[2m` (cyan dim)
- `-    expiry = datetime.now() + ...` : `\x1b[31m` (red)
- `+    expiry = datetime.now(timezone.utc) + ...` : `\x1b[32m` (green)
- Context lines: `\x1b[2m` (dim)

### 11.2 Compact Diff with Truncation Footer

```
─── src/auth/session.py ───────────────────────────────────────────────────────
@@ -1,3 +1,3 @@
-old line 1
+new line 1
 context line
  … +47 additions, -38 deletions  /expand a3f8b2c1
────────────────────────────────────────────────────────────────────────────────
```

### 11.3 Stats-Only (binary file)

```
  src/assets/logo.png  binary file, 24,892 bytes
```

### 11.4 Stats-Only (oversized file)

```
  src/data/large_dataset.csv  +1,823 additions, -0 deletions  (diff too large: 2.1 MB)
```

### 11.5 Expanded Diff with Line Numbers

```
─── src/auth/session.py (expanded) ────────────────────────────────────────────
 143  143      """Check if the session token is still valid."""
 144  144      try:
 145      -    expiry = datetime.now() + timedelta(hours=24)
      145 +    expiry = datetime.now(timezone.utc) + timedelta(hours=24)
 146  146      if not token.valid:
 147      -        if token.expiry < datetime.now():
      147 +        if token.expiry < datetime.now(timezone.utc):
 148  148              raise TokenExpiredError(f"Token {token.id} expired")
────────────────────────────────────────────────────────────────────────────────
```

---

*End of diff-viewer-prd.md*
