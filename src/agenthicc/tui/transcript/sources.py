"""What a transcript view shows: the session log, a text file or a document (TUX-09).

A source is a numbered list of *items*. The view asks it how many there are,
for the rows of one item at a width, and for the few facts navigation needs
(which items open a user turn, which call for attention). Everything else is
the source's business:

- :class:`SessionLogSource` indexes the session's JSON-lines log once on a
  worker thread, then loads small windows of events through the bounded page
  loader (``load_older_transcript_page``) as the view asks for them;
- :class:`TextFileSource` is the same for a plain text file, one item per line
  (``/logs``: no turn structure);
- :class:`DocumentSource` holds renderables that exist already (an approval's
  full arguments or diff), one item per rendered row.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from rich.console import Console
from rich.text import Text

from agenthicc.tui.conversation_store import ConversationEvent
from agenthicc.tui.runtime.session_log import (
    TranscriptCursor,
    TranscriptEntry,
    get_session_log_path,
    load_older_transcript_page,
    load_transcript_entry,
)
from agenthicc.tui.theme import palette
from agenthicc.tui.transcript.log import (
    EVENT_FORMAT,
    FOLLOW_BYTES,
    TEXT_FORMAT,
    LogFormat,
    LogIndex,
    SearchJob,
)
from agenthicc.tui.transcript.render import EventRenderer
from agenthicc.tui.transcript.text import plain_text

if TYPE_CHECKING:
    from rich.console import RenderableType

    from agenthicc.config import UISettings

__all__ = [
    "DocumentSource",
    "SessionLogSource",
    "TextFileSource",
    "TranscriptSource",
]

#: Events loaded around an item the view asks for, and the bytes they may hold.
_WINDOW_EVENTS = 48
_WINDOW_BYTES = 512 * 1024
_EVENT_BYTES = 32 * 1024
#: Windows kept loaded.
_WINDOWS_KEPT = 512
#: The longest text-file line read in full.
_LINE_BYTES = 64 * 1024

_EXPANDABLE = frozenset({"tool_complete", "file_modified"})


class TranscriptSource(Protocol):
    """The items of one view."""

    title: str
    #: Whether items open numbered user turns (``[``, ``]`` and ``/rewind`` apply).
    has_turns: bool
    #: Called from a worker thread when the index or a search has news.
    on_change: Callable[[], None]

    @property
    def ready(self) -> bool: ...

    def count(self) -> int: ...

    def fit(self, width: int) -> None:
        """Lay the items out for *width* (only a document's items depend on it)."""
        ...

    def refresh(self) -> int:
        """Pick up items appended since; return how many."""
        ...

    def rows(self, item: int, width: int, expanded: bool) -> list[Text]: ...

    def can_expand(self, item: int) -> bool: ...

    def turn_items(self) -> Sequence[int]: ...

    def attention_items(self) -> Sequence[int]: ...

    def turn_number(self, item: int) -> int: ...

    def plain_text(self, item: int) -> str: ...

    def copy_text(self, item: int) -> str:
        """What ``y`` copies and ``e`` opens for *item*."""
        ...

    def user_text(self, item: int) -> str:
        """The prompt of a user turn (``""`` for any other item): what ``/rewind`` resends."""
        ...

    def last_tool(self) -> int:
        """The last tool call's item, or -1 (where Ctrl+T opens)."""
        ...

    def start_search(self, query: str) -> SearchJob: ...

    def cancel_search(self) -> None: ...

    def close(self) -> None: ...


def _noop() -> None:
    return None


class _IndexedSource:
    """An indexed file: the scan and the searches run on worker threads."""

    has_turns = False

    def __init__(self, path: Path, fmt: LogFormat, title: str) -> None:
        self.title = title
        self.on_change: Callable[[], None] = _noop
        self.index = LogIndex(path, fmt)
        self._ready = threading.Event()
        self._cancel = threading.Event()
        self._job: SearchJob | None = None
        self._thread = threading.Thread(target=self._scan, name="transcript-index", daemon=True)
        self._thread.start()

    def _scan(self) -> None:
        try:
            self.index.scan(self._cancel)
        finally:
            self._ready.set()
            self.on_change()

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    def count(self) -> int:
        return self.index.count

    def fit(self, width: int) -> None:
        return None

    def refresh(self) -> int:
        """Index what was appended since (at most :data:`FOLLOW_BYTES` at a time)."""
        if not self.ready:
            return 0
        return self.index.scan(limit=FOLLOW_BYTES)

    def turn_items(self) -> Sequence[int]:
        return self.index.turn_items

    def attention_items(self) -> Sequence[int]:
        return self.index.attention_items

    def turn_number(self, item: int) -> int:
        return self.index.turn_number(item)

    def can_expand(self, item: int) -> bool:
        return False

    def plain_text(self, item: int) -> str:
        raise NotImplementedError

    def copy_text(self, item: int) -> str:
        return self.plain_text(item)

    def user_text(self, item: int) -> str:
        return ""

    def last_tool(self) -> int:
        return -1

    def start_search(self, query: str) -> SearchJob:
        self.cancel_search()
        job = SearchJob(query)
        self._job = job

        def run() -> None:
            self._ready.wait()
            job.hits = self.index.search(query, job.cancel)
            job.done = not job.cancel.is_set()
            self.on_change()

        threading.Thread(target=run, name="transcript-search", daemon=True).start()
        return job

    def cancel_search(self) -> None:
        job, self._job = self._job, None
        if job is not None:
            job.cancel.set()

    def close(self) -> None:
        self.cancel_search()
        self._cancel.set()
        self.on_change = _noop


class SessionLogSource(_IndexedSource):
    """The events of a session's log, loaded a window at a time."""

    has_turns = True

    def __init__(self, session_id: str, *, ui: UISettings | None = None, title: str = "") -> None:
        self.session_id = session_id
        self._renderer = EventRenderer(ui)
        self._windows: OrderedDict[int, TranscriptEntry] = OrderedDict()
        super().__init__(get_session_log_path(session_id), EVENT_FORMAT, title)

    # ── windows ──────────────────────────────────────────────────────────────

    def _remember(self, entry: TranscriptEntry) -> None:
        self._windows[entry.start_offset] = entry
        self._windows.move_to_end(entry.start_offset)
        while len(self._windows) > _WINDOWS_KEPT:
            self._windows.popitem(last=False)

    def entry(self, item: int) -> TranscriptEntry | None:
        """The loaded entry of *item*: one bounded window of events around it."""
        index = self.index
        if not 0 <= item < index.count:
            return None
        start = index.starts[item]
        cached = self._windows.get(start)
        if cached is not None:
            self._windows.move_to_end(start)
            return cached
        for last, size in (
            (min(item + _WINDOW_EVENTS // 2, index.count - 1), _WINDOW_EVENTS),
            (item, 1),
        ):
            page = load_older_transcript_page(
                self.session_id,
                TranscriptCursor(index.ends[last]),
                max_events=size,
                max_bytes=_WINDOW_BYTES,
                max_event_bytes=_EVENT_BYTES,
            )
            for entry in page.entries:
                self._remember(entry)
            if start in self._windows:
                return self._windows[start]
        return None

    def event(self, item: int, *, full: bool = False) -> ConversationEvent | None:
        """The event of *item*; with *full* a record too large for a window is read whole."""
        entry = self.entry(item)
        if entry is None:
            return None
        if full and (entry.oversized or entry.partial_line):
            return load_transcript_entry(entry) or entry.event
        return entry.event

    # ── what the view asks ───────────────────────────────────────────────────

    def rows(self, item: int, width: int, expanded: bool) -> list[Text]:
        event = self.event(item, full=expanded)
        if event is None:
            return [Text("(this event could not be read)", style=palette().muted)]
        return self._renderer.rows(event, width, expanded=expanded)

    def can_expand(self, item: int) -> bool:
        return self.index.kind(item) in _EXPANDABLE

    def plain_text(self, item: int) -> str:
        event = self.event(item, full=True)
        return plain_text(event.kind, event.payload) if event is not None else ""

    def kind(self, item: int) -> str:
        return self.index.kind(item)

    def last_tool(self) -> int:
        index = self.index
        for item in range(index.count - 1, -1, -1):
            if index.kind(item) == "tool_complete":
                return item
        return -1

    def user_text(self, item: int) -> str:
        """The text of a user message item (``""`` for any other)."""
        event = self.event(item, full=True)
        if event is None or event.kind != "user_message":
            return ""
        text = event.payload.get("text")
        return text if isinstance(text, str) else ""


class TextFileSource(_IndexedSource):
    """A text file, one item per line (the diagnostics log)."""

    def __init__(self, path: Path, title: str = "") -> None:
        self._consoles: dict[int, Console] = {}
        super().__init__(path, TEXT_FORMAT, title or str(path))

    def _line(self, item: int) -> str:
        start, end = self.index.starts[item], self.index.ends[item]
        try:
            with self.index.path.open("rb") as handle:
                handle.seek(start)
                raw = handle.read(min(end - start, _LINE_BYTES))
        except OSError:
            return ""
        return raw.decode("utf-8", "replace").rstrip("\r\n")

    def rows(self, item: int, width: int, expanded: bool) -> list[Text]:
        p = palette()
        line = self._line(item)
        tone = ""
        if self.index.flags[item]:
            tone = (
                p.danger
                if any(word in line for word in ("ERROR", "CRITICAL", "Traceback"))
                else p.warning
            )
        text = Text(line.expandtabs(4), style=tone, no_wrap=False)
        console = self._consoles.setdefault(width, Console(width=width, highlight=False))
        wrapped = text.wrap(console, width, overflow="fold")
        return list(wrapped) or [Text("")]

    def plain_text(self, item: int) -> str:
        return self._line(item)


class DocumentSource:
    """Renderables that exist already, one item per rendered row (an approval's details)."""

    has_turns = False

    def __init__(self, title: str, renderables: Sequence[RenderableType]) -> None:
        self.title = title
        self.on_change: Callable[[], None] = _noop
        self._renderables = list(renderables)
        self._rows: list[Text] = []
        self._width = 0
        self._job: SearchJob | None = None

    @property
    def ready(self) -> bool:
        return True

    def fit(self, width: int) -> None:
        if width != self._width:
            from agenthicc.tui.workspace.dialog import render_lines

            self._rows = render_lines(self._renderables, width)
            self._width = width

    def count(self) -> int:
        return len(self._rows)

    def refresh(self) -> int:
        return 0

    def rows(self, item: int, width: int, expanded: bool) -> list[Text]:
        return [self._rows[item].copy()] if 0 <= item < len(self._rows) else []

    def can_expand(self, item: int) -> bool:
        return False

    def turn_items(self) -> Sequence[int]:
        return ()

    def attention_items(self) -> Sequence[int]:
        return ()

    def turn_number(self, item: int) -> int:
        return 0

    def plain_text(self, item: int) -> str:
        return self._rows[item].plain if 0 <= item < len(self._rows) else ""

    def copy_text(self, item: int) -> str:
        """The whole document: a document's rows are one piece of text."""
        return "\n".join(row.plain for row in self._rows)

    def user_text(self, item: int) -> str:
        return ""

    def last_tool(self) -> int:
        return -1

    def start_search(self, query: str) -> SearchJob:
        from agenthicc.tui.transcript.text import matches

        job = SearchJob(query)
        job.hits = [i for i, row in enumerate(self._rows) if matches(query, row.plain)]
        job.done = True
        self._job = job
        return job

    def cancel_search(self) -> None:
        self._job = None

    def close(self) -> None:
        self.on_change = _noop
