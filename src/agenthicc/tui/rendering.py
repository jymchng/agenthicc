"""Width-safe rendering helpers for the TUI live panel.

Every component's ``render(cols)`` method should call these utilities so that
Rich markup strings never exceed the terminal width.  Overflowing lines desync
Rich's cursor-repositioning and cause the live panel to overwrite content above
it (the "havoc" bug described in PRD-56).

Usage::

    from agenthicc.tui.rendering import visible_len, fit

    # Check width before adding a segment:
    if visible_len(current) + visible_len(new_seg) <= cols:
        current += new_seg

    # Truncate to fit:
    safe = fit(some_markup, cols)
"""

from __future__ import annotations

import functools


@functools.lru_cache(maxsize=2048)
def visible_len(markup: str) -> int:
    """Return the number of terminal columns *markup* occupies.

    Strips Rich markup tags before measuring so ``[bold]Hi[/bold]`` correctly
    reports 2 columns rather than the raw string length. The status bar and
    footer measure the same strings every frame, so results are cached
    (AGX-11).
    """
    from rich.text import Text

    return Text.from_markup(markup, emoji=False).cell_len


@functools.lru_cache(maxsize=2048)
def fit(markup: str, cols: int, ellipsis: str = "…") -> str:
    """Truncate *markup* so it renders within *cols* terminal columns.

    When the content already fits, the original string is returned unchanged
    (zero cost).  When truncation is necessary the markup is stripped (styles
    are not preserved in the truncated region) and *ellipsis* is appended.

    The ellipsis character ``…`` is 1 terminal column wide.  Pass a different
    string (e.g. ``"..."`` for 3 columns) for wider terminals or preferences.
    """
    from rich.text import Text
    from rich.markup import escape

    t = Text.from_markup(markup, emoji=False)
    if t.cell_len <= cols:
        return markup

    ell_cells = Text(ellipsis).cell_len
    target = max(0, cols - ell_cells)
    t.truncate(target, overflow="fold")
    return escape(t.plain) + ellipsis
