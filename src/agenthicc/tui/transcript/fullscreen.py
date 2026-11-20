"""The fullscreen layout: a scrolling pane over a docked composer (TUX-09 §2.3).

``[ui] layout = "fullscreen"`` (and ``/fullscreen``) runs the session in the
alternate screen. The workspace keeps drawing the composer frame, the status
line and every overlay exactly as it does inline; this module adds the rows
above them, the *pane*:

- the pane is the transcript view's engine (:class:`TranscriptView`) over the
  session's own log, anchored to the tail while it follows;
- what the session prints straight to the console (the welcome header, a
  command's listing, a notice) is captured and shown in the pane in order with
  the events, so the pane's first item is the welcome header (TUX-03);
- PageUp/PageDown, Shift+↑/↓ and the mouse wheel scroll it; scrolling up
  stops following and shows ``↓ N new · End to follow``; End follows again;
- only the rows on screen are rendered, from a bounded cache of rendered items
  (AGX-11), so a log of 10,000 events costs what a screenful costs.

Leaving the layout prints the conversation to the normal screen in ``compact``
form (what the pane held since the layout began, console output included), so
the terminal's scrollback still has the session.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Literal

from rich.console import Console, ConsoleRenderable, RenderableType, RenderHook
from rich.control import Control
from rich.segment import Segments
from rich.text import Text

from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.theme import glyphs, key_arrows, style
from agenthicc.tui.transcript.log import SearchJob
from agenthicc.tui.transcript.sources import SessionLogSource
from agenthicc.tui.transcript.view import TranscriptView

if TYPE_CHECKING:
    from agenthicc.config import UISettings
    from agenthicc.tui.keymap import Keymap

__all__ = ["ConsoleCapture", "FullscreenLayout", "NotedSource", "PaneAction", "PaneView"]

PaneAction = Literal[
    "page_up", "page_down", "line_up", "line_down", "wheel_up", "wheel_down", "follow"
]
#: Rows one wheel notch scrolls.
_WHEEL_ROWS = 3
#: Events read per chunk when the exit transcript is printed.
_DUMP_CHUNK = 256
#: Console prints kept (the oldest are dropped from the pane, not from the log).
_MAX_NOTES = 500


class ConsoleCapture(RenderHook):
    """Takes what is printed to the console while the layout is on, for the pane."""

    def __init__(self, take: Callable[[RenderableType], None]) -> None:
        self._take = take

    def process_renderables(self, renderables: list[ConsoleRenderable]) -> list[ConsoleRenderable]:
        """Take every renderable but terminal control: frames and mode switches pass through."""
        passed: list[ConsoleRenderable] = []
        for renderable in renderables:
            if isinstance(renderable, (Segments, Control)):
                passed.append(renderable)
            else:
                self._take(renderable)
        return passed


class NotedSource:
    """A session log's items with the console's output interleaved at the moment it was printed."""

    has_turns = False

    def __init__(self, inner: SessionLogSource) -> None:
        self.inner = inner
        self.title = inner.title
        self.on_change: Callable[[], None] = inner.on_change
        #: ``(byte offset of the log's end when printed, what was printed)``.
        self.notes: list[tuple[int, RenderableType]] = []
        self._keys: list[int] = []
        self._synced = (-1, -1)
        self._rows: dict[tuple[int, int], list[Text]] = {}

    def note(self, renderable: RenderableType) -> None:
        """Keep *renderable*, which was just printed, after the log items before it."""
        if len(self.notes) >= _MAX_NOTES:
            del self.notes[0]
            self._rows.clear()
        self.notes.append((self.inner.index.size(), renderable))
        self._synced = (-1, -1)

    def positions(self) -> list[int]:
        """Each note's place in the log's items (the number of items before it)."""
        index = self.inner.index
        return [
            bisect.bisect_left(index.starts, offset, 0, index.count) for offset, _ in self.notes
        ]

    def _sync(self) -> None:
        state = (self.inner.count(), len(self.notes))
        if state != self._synced:
            self._keys = [pos + number for number, pos in enumerate(self.positions())]
            self._synced = state

    @property
    def ready(self) -> bool:
        return self.inner.ready

    def count(self) -> int:
        return self.inner.count() + len(self.notes)

    def fit(self, width: int) -> None:
        self.inner.fit(width)

    def refresh(self) -> int:
        return self.inner.refresh()

    def rows(self, item: int, width: int, expanded: bool) -> list[Text]:
        self._sync()
        at = bisect.bisect_left(self._keys, item)
        if at < len(self._keys) and self._keys[at] == item:
            return [row.copy() for row in self._note_rows(at, width)]
        return self.inner.rows(item - at, width, expanded)

    def _note_rows(self, number: int, width: int) -> list[Text]:
        key = (number, width)
        rows = self._rows.get(key)
        if rows is None:
            from agenthicc.tui.workspace.dialog import render_lines

            rows = render_lines([self.notes[number][1]], width)
            if len(self._rows) > 256:
                self._rows.clear()
            self._rows[key] = rows
        return rows

    def can_expand(self, item: int) -> bool:
        return False

    def turn_items(self) -> Sequence[int]:
        return ()

    def attention_items(self) -> Sequence[int]:
        return ()

    def turn_number(self, item: int) -> int:
        return 0

    def plain_text(self, item: int) -> str:
        return ""

    def copy_text(self, item: int) -> str:
        return ""

    def user_text(self, item: int) -> str:
        return ""

    def last_tool(self) -> int:
        return -1

    def start_search(self, query: str) -> SearchJob:
        job = SearchJob(query)  # the pane does not search: no hits
        job.done = True
        return job

    def cancel_search(self) -> None:
        self.inner.cancel_search()

    def close(self) -> None:
        self.inner.close()


class PaneView(TranscriptView):
    """The transcript engine without chrome or selection, following the tail."""

    def __init__(
        self,
        source: NotedSource,
        keymap: Keymap,
        *,
        on_scrolled: Callable[[bool], None] = lambda scrolled: None,
    ) -> None:
        super().__init__(source, keymap)
        self.follow = True
        self._seen = 0
        self._on_scrolled = on_scrolled

    # The pane has no title, rules or footer, and no gutter.
    @property
    def gutter(self) -> int:
        return 0

    @property
    def content_rows(self) -> int:
        return max(1, self.height)

    def _compose(self, item: int, index: int, body: Text) -> Text:
        row = body.copy()
        row.no_wrap = True
        row.overflow = "crop"
        return row

    @property
    def unseen(self) -> int:
        """Items added since the pane stopped following."""
        return 0 if self.follow else max(0, self._count() - self._seen)

    def _set_follow(self, follow: bool) -> None:
        if follow == self.follow:
            return
        self.follow = follow
        self._seen = self._count()
        self._on_scrolled(not follow)

    def scroll(self, action: PaneAction) -> None:
        """Scroll the pane; scrolling up stops following, reaching the end resumes."""
        rows = max(1, self.content_rows - 1)
        match action:
            case "page_up":
                self._scroll_rows(-rows)
            case "page_down":
                self._scroll_rows(rows)
            case "line_up":
                self._scroll_rows(-1)
            case "line_down":
                self._scroll_rows(1)
            case "wheel_up":
                self._scroll_rows(-_WHEEL_ROWS)
            case "wheel_down":
                self._scroll_rows(_WHEEL_ROWS)
            case "follow":
                self._set_follow(True)
                return
        if action in ("page_up", "line_up", "wheel_up"):
            self._set_follow(False)
        elif self._count() and self._at_bottom():
            self._set_follow(True)

    def _at_bottom(self) -> bool:
        """Whether the last row of the last item is on screen."""
        count = self._count()
        used = -self.top_skip
        item = self.top
        while item < count and used <= self.content_rows:
            used += self._height(item)
            item += 1
        return item >= count and used <= self.content_rows

    def render_pane(self, width: int, height: int) -> list[Text]:
        """Exactly *height* rows of at most *width* cells."""
        self.width, self.height = max(10, width), max(1, height)
        self.source.fit(self._room())
        self.source.refresh()
        count = self._count()
        if count and self.follow:
            self._anchor_bottom(count - 1)
        else:
            self._clamp()
        rows = self._content()
        if not self.follow:
            rows[-1] = self._indicator(rows[-1])
        return rows

    def _indicator(self, row: Text) -> Text:
        """The last pane row, ending in ``↓ N new · End to follow``."""
        g = glyphs()
        hint = self.keymap.hint("composer.pane_follow")
        key = key_arrows(hint.keys) if hint is not None else "End"
        unseen = self.unseen
        text = f" {g.down} {f'{unseen} new {g.sep} ' if unseen else ''}{key} to follow "
        cut = max(0, self.width - len(text))
        kept = row.copy()
        kept.truncate(cut, pad=True)
        kept.append(text, style("accent", bold=True))
        return kept


class FullscreenLayout:
    """The pane's state for one stay in the fullscreen layout."""

    def __init__(
        self,
        session_id: str,
        keymap: Callable[[], Keymap],
        *,
        ui: UISettings | None = None,
        on_scrolled: Callable[[bool], None] = lambda scrolled: None,
    ) -> None:
        self.session_id = session_id
        self.source = SessionLogSource(session_id, ui=ui)
        self.noted = NotedSource(self.source)
        self.pane = PaneView(self.noted, keymap(), on_scrolled=on_scrolled)
        self.capture = ConsoleCapture(self.noted.note)
        #: The log's size when the layout began: what is before it is already in
        #: the terminal's scrollback (or was never printed, on a resume).
        self.entry_offset = self.source.index.size()

    # ── frames ───────────────────────────────────────────────────────────────

    def rows(self, width: int, height: int) -> list[Text]:
        return self.pane.render_pane(width, height)

    def scroll(self, action: PaneAction) -> None:
        self.pane.scroll(action)

    @property
    def scrolled(self) -> bool:
        return not self.pane.follow

    # ── leaving ──────────────────────────────────────────────────────────────

    def settle(self, timeout: float = 5.0) -> None:
        """Wait for the log index and take the events appended since."""
        self.source.wait_ready(timeout)
        self.source.refresh()

    def _events(self, first: int, last: int) -> list[ConversationEvent]:
        events: list[ConversationEvent] = []
        for item in range(first, last):
            event = self.source.event(item, full=False)
            if event is not None:
                events.append(event)
        return events

    def print_exit_transcript(self, console: Console) -> int:
        """Print the pane's conversation to *console* in compact form; return the events printed.

        Events and the console output captured between them are printed in the
        order they happened, through a private appender at the ``compact`` level.
        """
        from agenthicc.tui.workspace.appender import ScrollBufferAppender

        self.settle()
        index = self.source.index
        first = bisect.bisect_left(index.starts, self.entry_offset, 0, index.count)
        appender = ScrollBufferAppender(
            AppState(), console, max_live_tool_calls=10**9, fixed_detail="compact"
        )
        printed = 0
        cursor = first

        def events_until(end: int) -> None:
            nonlocal cursor, printed
            while cursor < end:
                stop = min(end, cursor + _DUMP_CHUNK)
                chunk = self._events(cursor, stop)
                printed += len(chunk)
                appender.print_events(chunk)
                cursor = stop

        for position, (_offset, note) in zip(self.noted.positions(), self.noted.notes, strict=True):
            events_until(max(first, position))
            console.print(note)
        events_until(index.count)
        self.entry_offset = index.scanned
        return printed

    def close(self) -> None:
        self.source.close()
