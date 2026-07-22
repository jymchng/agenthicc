"""GitHub/VS Code–style unified diff renderer for the Rich terminal (PRD-101).

Public API
----------
render_file_diff(path, old_lines, new_lines, *, context, language, operation)
    → Rich renderable (Group)

Visual output
-------------
● Update(src/path/to/file.py)
└─ Added 1 line, removed 1 line

264     # context line
265     from agenthicc.tui.runtime.mode_manager import build_mode_str
266     mode = self._state.active_mode()
267 -   mode_line = _fit(f"  [dim]{build_mode_str(mode)}[/dim]", cols)
267 +   mode_line = _fit(f"  {build_mode_str(mode)}", cols)
268
269     # next context line
"""

from __future__ import annotations

import difflib
from io import StringIO
from typing import TYPE_CHECKING

from rich.console import Console
from rich.console import Group

if TYPE_CHECKING:
    from rich.console import RenderableType
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

__all__ = ["render_file_diff", "render_file_create"]

#: Maximum lines shown in a file-creation preview (FR: show 10 lines).
CREATE_PREVIEW_LINES: int = 10

# ── palette ───────────────────────────────────────────────────────────────────

_BG_DEL = "on #3a1515"
_BG_ADD = "on #153a15"
_BG_DEL_WORD = "on #6a2020"
_BG_ADD_WORD = "on #206a20"


# ── syntax highlighting ───────────────────────────────────────────────────────


def _highlight_block(lines: list[str], language: str) -> list[Text]:
    """Syntax-highlight *lines* as a block; return one ``Text`` per line.

    Rendering as a block preserves multi-line syntax context (e.g. open
    string literals, decorator chains) that per-line rendering would miss.
    """
    if not lines:
        return []
    from rich.syntax import Syntax  # noqa: PLC0415

    buf = StringIO()
    con = Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        width=10_000,
        highlight=False,
    )
    with con.capture() as cap:
        con.print(
            Syntax("\n".join(lines), language, theme="monokai", line_numbers=False),
            end="",
        )
    raw = cap.get().splitlines()
    # Syntax may emit a trailing blank line; pad/trim to match input count.
    while len(raw) < len(lines):
        raw.append("")
    return [Text.from_ansi(raw[i]) for i in range(len(lines))]


# ── word-level diff ───────────────────────────────────────────────────────────


def _word_spans(
    old: str,
    new: str,
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Compute changed character regions between *old* and *new*.

    Returns ``(removed_spans, added_spans)`` as lists of ``(start, end)``
    half-open index pairs into the respective strings.
    """
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    removed: list[tuple[int, int]] = []
    added: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed.append((i1, i2))
        if tag in ("replace", "insert"):
            added.append((j1, j2))
    return removed, added


# ── header / summary ──────────────────────────────────────────────────────────


def _header(path: str, operation: str) -> Text:
    return Text.assemble(
        ("● ", "green"),
        (f"{operation}(", "bold"),
        (path, "blue underline"),
        (")", "bold"),
    )


def _create_summary(n_lines: int) -> Text:
    word = "line" if n_lines == 1 else "lines"
    return Text.assemble(
        ("└─ ", "dim"),
        (f"Created {n_lines} {word}", "green"),
    )


def _summary(n_added: int, n_removed: int) -> Text:
    parts: list[tuple[str, str]] = [("└─ ", "dim")]
    if n_added:
        word = "line" if n_added == 1 else "lines"
        parts.append((f"Added {n_added} {word}", "green"))
    if n_added and n_removed:
        parts.append((", ", "dim"))
    if n_removed:
        word = "line" if n_removed == 1 else "lines"
        parts.append((f"removed {n_removed} {word}", "red"))
    if not n_added and not n_removed:
        parts.append(("no changes", "dim"))
    return Text.assemble(*parts)


# ── table rows ────────────────────────────────────────────────────────────────


def _context_row(table: Table, lineno: int, hl: Text) -> None:
    table.add_row(
        Text(str(lineno), style="bright_black"),
        Text(" "),
        hl,
    )


def _gap_row(table: Table) -> None:
    table.add_row(
        Text("⋯", style="dim"),
        Text(" "),
        Text(""),
    )


def _del_row(
    table: Table,
    lineno: int,
    raw: str,
    hl: Text,
    pair_raw: str | None,
) -> None:
    code = hl.copy()
    code.stylize(_BG_DEL)
    if pair_raw is not None:
        for s, e in _word_spans(raw, pair_raw)[0]:
            code.stylize(_BG_DEL_WORD, s, e)
    table.add_row(
        Text(str(lineno), style=f"red {_BG_DEL}"),
        Text("-", style=f"red {_BG_DEL}"),
        code,
        style=_BG_DEL,
    )


def _add_row(
    table: Table,
    lineno: int,
    raw: str,
    hl: Text,
    pair_raw: str | None,
) -> None:
    code = hl.copy()
    code.stylize(_BG_ADD)
    if pair_raw is not None:
        for s, e in _word_spans(pair_raw, raw)[1]:
            code.stylize(_BG_ADD_WORD, s, e)
    table.add_row(
        Text(str(lineno), style=f"green {_BG_ADD}"),
        Text("+", style=f"green {_BG_ADD}"),
        code,
        style=_BG_ADD,
    )


# ── hunk grouping ────────────────────────────────────────────────────────────


def _build_hunks(
    opcodes: list[tuple],
    context: int,
) -> list[list[tuple]]:
    """Group opcodes into hunks separated by equal gaps larger than 2*context.

    Each hunk is a flat list of opcodes with equal blocks pre-trimmed to at
    most *context* lines at both edges.  The rendering loop can render every
    opcode in a hunk in full — all context logic lives here, not there.

    Algorithm
    ---------
    * Short equal blocks (≤ 2*context) stay in the current hunk unchanged.
    * Long equal blocks close the current hunk (appending its trailing
      *context* lines), then store the block's leading *context* lines as
      ``pending_context`` for the next hunk.
    * ``pending_context`` is consumed the moment a non-equal opcode arrives.
      If the file ends without another change, it is discarded — no spurious
      trailing-context-only hunk is emitted.
    """
    hunks: list[list[tuple]] = []
    current: list[tuple] = []
    pending_context: tuple | None = None

    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            n = i2 - i1
            if n <= 2 * context:
                # Short gap — absorb into the current hunk as-is.
                if pending_context is not None:
                    current.append(pending_context)
                    pending_context = None
                current.append((tag, i1, i2, j1, j2))
            else:
                # Long gap — close current hunk with trailing context, then
                # park leading context for the hunk that follows.
                if current:
                    current.append(("equal", i1, i1 + context, j1, j1 + context))
                    hunks.append(current)
                    current = []
                pending_context = ("equal", i2 - context, i2, j2 - context, j2)
        else:
            if pending_context is not None:
                current.append(pending_context)
                pending_context = None
            current.append((tag, i1, i2, j1, j2))

    if current:
        hunks.append(current)
    # pending_context is discarded — no change followed it.

    # Trim the leading equal block of the first hunk (file starts with equal
    # lines shorter than 2*context — keep only the last `context` of them).
    if hunks and hunks[0] and hunks[0][0][0] == "equal":
        tag, i1, i2, j1, j2 = hunks[0][0]
        n = i2 - i1
        if n > context:
            hunks[0][0] = ("equal", i2 - context, i2, j2 - context, j2)

    # Trim the trailing equal block of the last hunk symmetrically.
    if hunks and hunks[-1] and hunks[-1][-1][0] == "equal":
        tag, i1, i2, j1, j2 = hunks[-1][-1]
        n = i2 - i1
        if n > context:
            hunks[-1][-1] = ("equal", i1, i1 + context, j1, j1 + context)

    return hunks


# ── public API ────────────────────────────────────────────────────────────────


def render_file_diff(
    path: str,
    old_lines: list[str],
    new_lines: list[str],
    *,
    context: int = 3,
    language: str = "python",
    operation: str = "Update",
) -> "RenderableType":
    """Render a GitHub-style diff between *old_lines* and *new_lines*.

    Parameters
    ----------
    path:
        File path shown in the header.
    old_lines / new_lines:
        The before/after line lists (without trailing newlines).
    context:
        Number of unchanged lines to show either side of each hunk.
    language:
        Pygments language name for syntax highlighting.
    operation:
        Verb shown in the header bullet (``"Update"``, ``"Create"``, …).
    """
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    opcodes = sm.get_opcodes()

    n_added = sum(j2 - j1 for t, _, _, j1, j2 in opcodes if t in ("insert", "replace"))
    n_removed = sum(i2 - i1 for t, i1, i2, _, _ in opcodes if t in ("delete", "replace"))

    # Pre-highlight both versions as blocks so syntax context is correct.
    old_hl = _highlight_block(old_lines, language)
    new_hl = _highlight_block(new_lines, language)

    table = Table.grid(padding=(0, 1))
    table.add_column(justify="right", no_wrap=True)  # line number
    table.add_column(justify="center", no_wrap=True)  # +/-
    table.add_column(no_wrap=False)  # code

    if not (n_added or n_removed):
        # Identical files — show everything with no markers.
        for lineno, hl in enumerate(old_hl, 1):
            _context_row(table, lineno, hl)
    else:
        hunks = _build_hunks(opcodes, context)
        for hunk_idx, hunk in enumerate(hunks):
            if hunk_idx > 0:
                _gap_row(table)
            for tag, i1, i2, j1, j2 in hunk:
                if tag == "equal":
                    for off in range(i2 - i1):
                        _context_row(table, i1 + off + 1, old_hl[i1 + off])
                else:
                    del_lines = old_lines[i1:i2]
                    add_lines = new_lines[j1:j2]
                    del_hl = old_hl[i1:i2]
                    add_hl = new_hl[j1:j2]
                    for idx, (raw, hl) in enumerate(zip(del_lines, del_hl)):
                        pair = add_lines[idx] if idx < len(add_lines) else None
                        _del_row(table, i1 + idx + 1, raw, hl, pair)
                    for idx, (raw, hl) in enumerate(zip(add_lines, add_hl)):
                        pair = del_lines[idx] if idx < len(del_lines) else None
                        _add_row(table, j1 + idx + 1, raw, hl, pair)

    return Group(
        _header(path, operation),
        _summary(n_added, n_removed),
        Text(""),
        Padding(table, pad=(0, 0, 0, 2)),
    )


def render_file_create(
    path: str,
    new_lines: list[str],
    *,
    max_lines: int = CREATE_PREVIEW_LINES,
    language: str = "python",
) -> "RenderableType":
    """Render a file-creation event showing at most *max_lines* lines.

    Visual output::

        ● Create(src/path/to/file.py)
        └─ Created 42 lines

        1 + def hello():
        2 +     return "world"
        ⋯ +40 more lines
    """
    n_total = len(new_lines)
    preview = new_lines[:max_lines]
    hl = _highlight_block(preview, language)

    table = Table.grid(padding=(0, 1))
    table.add_column(justify="right", no_wrap=True)  # line number
    table.add_column(justify="center", no_wrap=True)  # +
    table.add_column(no_wrap=False)  # code

    for idx, (raw, h) in enumerate(zip(preview, hl)):
        _add_row(table, idx + 1, raw, h, None)  # pure add — no del pair

    parts: list[RenderableType] = [
        _header(path, "Create"),
        _create_summary(n_total),
        Text(""),
        Padding(table, pad=(0, 0, 0, 2)),
    ]
    if n_total > max_lines:
        parts.append(
            Padding(
                Text.assemble(
                    ("⋯ ", "dim"),
                    (f"+{n_total - max_lines} more lines", "dim green"),
                ),
                pad=(0, 0, 0, 2),
            )
        )
    return Group(*parts)
