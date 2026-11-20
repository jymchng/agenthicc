"""One logged event as rows, drawn by the scrollback's own renderers (TUX-09).

The live transcript is printed by :class:`ScrollBufferAppender` (TUX-06). The
view does not keep a second set of renderers: it gives each event a private
appender that prints into a recording console, and reads the styled rows back.
Collapsed items use the ``compact`` detail level; an expanded item uses
``verbose`` and adds the call's arguments. Output is cut at 64 KiB and says so.

The console's width is the room the view gives the item, so nothing wraps
twice.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from rich.console import Console
from rich.segment import Segment
from rich.table import Table
from rich.text import Text

from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.theme import glyphs, palette

if TYPE_CHECKING:
    from agenthicc.config import UISettings

__all__ = ["EXPAND_LIMIT_BYTES", "EventRenderer", "path_label", "rows_of"]

#: Output shown for one expanded item (the old history overlay's detail cap).
EXPAND_LIMIT_BYTES = 64 * 1024


class _RecordingConsole(Console):
    """A console whose printed segments can be taken back, one print at a time."""

    def take_segments(self) -> list[Segment]:
        with self._record_buffer_lock:
            segments = list(self._record_buffer)
            self._record_buffer.clear()
        return segments


def path_label(path: str) -> Text:
    """A file path as the view's title shows it.

    The one place the view itself prints a path (the ``/logs`` title). TUX-10's
    OSC 8 hyperlinks wrap it here; paths inside tool rows and diffs come from
    the scrollback renderers, which that change links at their source.
    """
    return Text(path, style=palette().muted, no_wrap=True, overflow="ellipsis")


def rows_of(segments: list[Segment]) -> list[Text]:
    """The styled :class:`Text` of each printed line (control codes dropped)."""
    rows: list[Text] = []
    for line in Segment.split_lines(segments):
        row = Text(no_wrap=True, overflow="ellipsis")
        for segment in line:
            if not segment.control:
                row.append(segment.text, segment.style)
        rows.append(row)
    return rows


def _trim(rows: list[Text]) -> list[Text]:
    """Drop the blank rows a block prints after itself: the view has its own spacing."""
    while rows and not rows[-1].plain.strip():
        rows.pop()
    return rows


def _bounded_output(payload: Mapping[str, object]) -> tuple[dict[str, object], bool]:
    """*payload* with its output cut at :data:`EXPAND_LIMIT_BYTES`, and whether it was."""
    lines = payload.get("output_lines")
    if not isinstance(lines, list):
        return dict(payload), False
    kept: list[object] = []
    used = 0
    for line in lines:
        used += len(str(line).encode("utf-8", "replace")) + 1
        if used > EXPAND_LIMIT_BYTES:
            return {**payload, "output_lines": kept}, True
        kept.append(line)
    return dict(payload), False


class EventRenderer:
    """Render events at a width; one instance serves one view."""

    def __init__(self, ui: UISettings | None = None) -> None:
        # A private state: the appender's group counters and live overflow
        # signal must never reach the session's own.
        self._state = AppState()
        if ui is not None:
            self._state.ui.set(ui)
        self._consoles: dict[int, _RecordingConsole] = {}

    def _console(self, width: int) -> _RecordingConsole:
        console = self._consoles.get(width)
        if console is None:
            if len(self._consoles) > 8:
                self._consoles.clear()
            console = _RecordingConsole(
                file=io.StringIO(),
                width=width,
                # A fixed height too: without one every ``size`` asks the terminal.
                height=1_000,
                force_terminal=True,
                color_system="truecolor",
                highlight=False,
                emoji=False,
                record=True,
                legacy_windows=False,
            )
            self._consoles[width] = console
        return console

    def rows(self, event: ConversationEvent, width: int, *, expanded: bool) -> list[Text]:
        """The rows *event* prints at *width* (no rows for an event the scrollback skips)."""
        from agenthicc.tui.workspace.appender import ScrollBufferAppender

        width = max(10, width)
        console = self._console(width)
        console.take_segments()
        payload: Mapping[str, object] = event.payload
        truncated = False
        if expanded and event.kind == "tool_complete":
            payload, truncated = _bounded_output(payload)
        shown = ConversationEvent(event.event_id, event.kind, dict(payload), event.timestamp)
        appender = ScrollBufferAppender(
            self._state,
            console,
            max_live_tool_calls=10**9,
            group_exploratory_calls=False,
            fixed_detail="verbose" if expanded else "compact",
            wrap=True,
        )
        appender.print_events([shown])
        if expanded and event.kind == "tool_complete":
            appender.flush()
            _print_arguments(console, event.payload)
        rows = _retarget_hint(_trim(rows_of(console.take_segments())), expanded=expanded)
        if truncated:
            rows.append(
                Text(f"  {glyphs().ellipsis} output truncated at 64 KiB", style=palette().muted)
            )
        return rows


def _retarget_hint(rows: list[Text], *, expanded: bool) -> list[Text]:
    """The scrollback's ``ctrl+t to expand`` footer says ``enter`` here, and nothing once open."""
    from agenthicc.tui.workspace.appender import EXPAND_HINT

    p, g = palette(), glyphs()
    kept: list[Text] = []
    for row in rows:
        plain = row.plain.rstrip()
        if not plain.endswith(EXPAND_HINT):
            kept.append(row)
            continue
        # A wrapped row is padded to the width; cut the padding with the hint.
        row.right_crop(len(row.plain) - len(plain) + len(EXPAND_HINT))
        if not expanded:
            row.append("enter to expand", p.muted)
            kept.append(row)
            continue
        separator = f"{g.sep} "
        if row.plain.endswith(separator):
            row.right_crop(len(separator))
        if row.plain.strip() not in ("", g.elbow):
            kept.append(row)
    return kept


def _print_arguments(console: Console, payload: Mapping[str, object]) -> None:
    """``arguments`` and one ``name  value`` entry for each, in full and wrapped.

    The values are what the call was given, already bounded when the event was
    written; a value of several lines keeps them, and a long line folds.
    """
    args = payload.get("args")
    if not isinstance(args, Mapping) or not args:
        return
    p, g = palette(), glyphs()
    console.print(Text(f"  {g.elbow} arguments", style=p.muted), highlight=False)
    names = [str(name) for name in args]
    label = min(24, max(len(name) for name in names)) + 2
    grid = Table.grid(expand=True)
    grid.add_column(width=6 + label, no_wrap=True)
    grid.add_column(ratio=1, overflow="fold")
    used = 0
    for name, value in args.items():
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        used += len(shown.encode("utf-8", "replace"))
        if used > EXPAND_LIMIT_BYTES:
            shown = shown[: max(0, EXPAND_LIMIT_BYTES - (used - len(shown)))]
        grid.add_row(
            Text(f"      {str(name)[:24]}:".ljust(6 + label), style=p.muted),
            Text(shown, style=p.text, overflow="fold"),
        )
        if used > EXPAND_LIMIT_BYTES:
            grid.add_row(Text(""), Text(f"{g.ellipsis} cut at 64 KiB", style=p.muted))
            break
    console.print(grid, highlight=False)
