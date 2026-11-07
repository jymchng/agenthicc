"""Queued-message rows above the composer (TUX-04 §2.6).

While a turn runs, each message typed for after it shows as one muted row
(at most :data:`MAX_ROWS`, then ``+N more``)::

     │ 1  also update the changelog
     → 2  check the lockfile too        at next tool

An interjection (Ctrl+Enter / Alt+Enter) is marked with an arrow and is
delivered at the running turn's next tool boundary. After ↑ on an empty prompt
the selected row is highlighted and a hint row lists the queue's keys.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.tui.conversation_store import AppState, QueuedMessage

__all__ = ["MAX_ROWS", "queue_rows"]

#: Queued messages shown before ``+N more``.
MAX_ROWS = 3


def _clip(text: str, width: int, ellipsis: str) -> str:
    """*text* cut to *width* terminal cells, ending in *ellipsis* when cut."""
    from rich.cells import cell_len

    if cell_len(text) <= width:
        return text
    budget = max(0, width - cell_len(ellipsis))
    out = ""
    for ch in text:
        if cell_len(out + ch) > budget:
            break
        out += ch
    return out + ellipsis


def _window(items: tuple[QueuedMessage, ...], focus: int | None) -> tuple[int, int]:
    """The ``[start, end)`` slice of *items* to draw, keeping the focus visible."""
    if len(items) <= MAX_ROWS:
        return 0, len(items)
    index = next((i for i, item in enumerate(items) if item.id == focus), 0)
    start = min(max(0, index - MAX_ROWS + 1), len(items) - MAX_ROWS)
    return start, start + MAX_ROWS


def queue_rows(state: AppState, cols: int) -> list[str]:
    """Markup rows for the queued messages; empty when nothing is queued."""
    from rich.markup import escape

    from agenthicc.tui.theme import glyphs, palette

    queue = state.conversation.queue
    items = queue.items()
    if not items:
        return []
    focus = queue.focus()
    p = palette()
    g = glyphs()
    start, end = _window(items, focus)
    rows: list[str] = []
    for number, item in enumerate(items[start:end], start + 1):
        lines = item.text.splitlines() or [""]
        first = lines[0] + (f" {g.ellipsis}" if len(lines) > 1 else "")
        marker = g.right if item.interject else g.pipe
        tag = "  at next tool" if item.interject else ""
        prefix = f"  {marker} {number}  "
        body = _clip(first, max(8, cols - len(prefix) - len(tag) - 1), g.ellipsis)
        if item.id == focus:
            rows.append(
                f"[bold][{p.accent}]{escape(prefix + body)}[/][/][{p.muted}]{escape(tag)}[/]"
            )
        else:
            rows.append(f"[{p.muted}]{escape(prefix + body + tag)}[/]")
    hidden = len(items) - (end - start)
    if hidden:
        rows.append(f"[{p.muted}]    +{hidden} more[/]")
    if focus is None and not state.input.buf() and state.conversation.is_running():
        # The queue rows carry their own entry hint: ↑ edit queue. ↑ selects
        # a row only during a turn (while the runtime loads it recalls history).
        hint = state.keymap().hint("composer.queue_focus")
        if hint is not None:
            rows[-1] += f"[{p.muted}]   {escape(hint.keys)} {escape(hint.label)}[/]"
    if focus is not None:
        hints = f" {g.sep} ".join(
            f"{hint.keys} {hint.label}" for hint in state.keymap().hints("queue")
        )
        rows.append(f"[{p.muted}]    {escape(hints)}[/]")
    return rows
