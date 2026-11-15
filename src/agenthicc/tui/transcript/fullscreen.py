"""The fullscreen layout: a scrolling pane over a docked composer (TUX-09 §2.3).

``[ui] layout = "fullscreen"`` (and ``/fullscreen``) runs the session in the
alternate screen. The workspace keeps drawing the composer frame, the status
line and every overlay exactly as it does inline; this module adds the rows
above them, the *pane*:

- the pane is the transcript view's engine (:class:`TranscriptView`) over the
  session's own log, anchored to the tail while it follows;
- the welcome header is its first item;
- PageUp/PageDown, Shift+↑/↓ and the mouse wheel scroll it; scrolling up
  stops following and shows ``↓ N new · End to follow``; End follows again;
- only the rows on screen are rendered, from a bounded cache of rendered items
  (AGX-11), so a log of 10,000 events costs what a screenful costs.

Leaving the layout prints the conversation to the normal screen in ``compact``
form (what the pane held since the layout began), so the terminal's scrollback
still has the session.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Iterator, Sequence
from typing import TYPE_CHECKING, Literal

from rich.console import Console
from rich.style import Style
from rich.text import Text

from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.theme import glyphs, palette, style
from agenthicc.tui.transcript.log import SearchJob
from agenthicc.tui.transcript.sources import SessionLogSource, TranscriptSource
from agenthicc.tui.transcript.view import TranscriptView

if TYPE_CHECKING:
    from agenthicc.config import UISettings
    from agenthicc.tui.keymap import Keymap
    from agenthicc.tui.welcome import WelcomeInfo

__all__ = ["FullscreenLayout", "HeaderedSource", "PaneAction", "PaneView"]

PaneAction = Literal[
    "page_up", "page_down", "line_up", "line_down", "wheel_up", "wheel_down", "follow"
]
#: Rows one wheel notch scrolls.
_WHEEL_ROWS = 3

#: Events read per chunk when the exit transcript is printed.
_DUMP_CHUNK = 256


class HeaderedSource:
    """A source whose first item is a header (the welcome rows)."""

    has_turns = False

    def __init__(self, inner: TranscriptSource, header: Callable[[int], list[Text]]) -> None:
        self.inner = inner
        self._header = header
        self._rows: list[Text] = []
        self._width = 0
        self.title = inner.title
        self.on_change: Callable[[], None] = inner.on_change

    _offset = 1

    def refit(self) -> None:
        """Ask the header for its rows again (the welcome changed)."""
        self._width = 0

    @property
    def ready(self) -> bool:
        return self.inner.ready

    def count(self) -> int:
        return self.inner.count() + self._offset

    def fit(self, width: int) -> None:
        self.inner.fit(width)
        if width != self._width:
            self._rows = self._header(width)
            self._width = width

    def refresh(self) -> int:
        return self.inner.refresh()

    def rows(self, item: int, width: int, expanded: bool) -> list[Text]:
        if item == 0:
            return [row.copy() for row in self._rows]
        return self.inner.rows(item - self._offset, width, expanded)

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
        source: HeaderedSource,
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
        count = self._count()
        if action in ("page_up", "line_up", "wheel_up"):
            self._set_follow(False)
        elif count and self._at_bottom():
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
        p, g = palette(), glyphs()
        hint = self.keymap.hint("composer.pane_follow")
        key = hint.keys if hint is not None else "End"
        unseen = self.unseen
        text = f" {g.down} {f'{unseen} new {g.sep} ' if unseen else ''}{key} to follow "
        cut = max(0, self.width - len(text))
        kept = row.copy()
        kept.truncate(cut, pad=True)
        kept.append(text, style("accent", bold=True) + Style.parse(p.on("accent_bg")))
        return kept


class FullscreenLayout:
    """The pane's state for one stay in the fullscreen layout."""

    def __init__(
        self,
        session_id: str,
        keymap: Callable[[], Keymap],
        *,
        ui: UISettings | None = None,
        welcome: WelcomeInfo | None = None,
        welcome_printed: bool = True,
        on_scrolled: Callable[[bool], None] = lambda scrolled: None,
    ) -> None:
        self.session_id = session_id
        self.welcome = welcome
        #: Whether the header is already in the terminal's scrollback.
        self.welcome_printed = welcome_printed
        self.source = SessionLogSource(session_id, ui=ui)
        self._header_source = HeaderedSource(self.source, self._header_rows)
        self.pane = PaneView(self._header_source, keymap(), on_scrolled=on_scrolled)
        #: The log's size when the layout began: what is before it is already in
        #: the terminal's scrollback (or was never printed, on a resume).
        self.entry_offset = self.source.index.size()

    def set_welcome(self, info: WelcomeInfo, *, printed: bool) -> None:
        """Show *info* as the pane's first item (it arrives after the first frame)."""
        self.welcome = info
        self.welcome_printed = printed
        self._header_source.refit()
        self.pane._invalidate()

    def _header_rows(self, width: int) -> list[Text]:
        from agenthicc.tui.welcome import render_welcome
        from agenthicc.tui.workspace.dialog import render_lines

        info = self.welcome
        if info is None:
            return []
        rows = render_lines([render_welcome(info, width)], width)
        return [*rows, Text("")]

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

    def exit_events(self) -> Iterator[list[ConversationEvent]]:
        """The events the terminal's scrollback has not seen, in chunks."""
        self.settle()
        index = self.source.index
        count = index.count
        first = bisect.bisect_left(index.starts, self.entry_offset, 0, count)
        chunk: list[ConversationEvent] = []
        for item in range(first, count):
            event = self.source.event(item, full=False)
            if event is None:
                continue
            chunk.append(event)
            if len(chunk) >= _DUMP_CHUNK:
                yield chunk
                chunk = []
        if chunk:
            yield chunk

    def print_exit_transcript(self, console: Console) -> int:
        """Print the pane's conversation to *console* in compact form; return the events printed."""
        from agenthicc.tui.workspace.appender import ScrollBufferAppender

        printed = 0
        if self.welcome is not None and not self.welcome_printed:
            from agenthicc.tui.welcome import print_welcome

            print_welcome(console, self.welcome)
            self.welcome_printed = True
        # A private state: the appender's counters never reach the session's own.
        appender = ScrollBufferAppender(
            AppState(), console, max_live_tool_calls=10**9, fixed_detail="compact"
        )
        for chunk in self.exit_events():
            printed += len(chunk)
            appender.print_events(chunk)
        self.entry_offset = self.source.index.scanned
        return printed

    def close(self) -> None:
        self.source.close()
