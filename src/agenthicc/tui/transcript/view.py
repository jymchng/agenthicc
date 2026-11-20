"""The transcript view: selection, scrolling, search, expansion and chrome (TUX-09 §2.1).

:class:`TranscriptView` is the engine. It holds no terminal: a host (the
alternate-screen loop in :mod:`~agenthicc.tui.transcript.host`, or the fullscreen
pane) feeds it keys and asks it for rows.

```text
 Transcript · fix flaky scheduler test · 14 turns                         q close
 ──────────────────────────────────────────────────────────────────────────────
  12 ❯ why does test_due_cursor fail on DST days?
     ● Read schedules/recurrence.py:80-140
     ✗ $ uv run pytest tests/unit/test_recurrence.py -q              exit 1 · 3.2s
 ▌   │ E   AssertionError: 2026-03-29 02:30 not in …                   [expanded]
 ──────────────────────────────────────────────────────────────────────────────
  ↑↓ move · Enter expand · [] turns · / search · n next hit · y copy · e pager
```

Only the rows on screen are built. The view scrolls by *anchor*: the item at
the top and how many of its rows are cut off. Moving the selection, paging and
following the tail all work from the anchor and render the few items they cross,
so a log of 10,000 events costs what a screenful costs. Rendered rows are kept
in a bounded cache keyed by item, width, expansion, search and theme.
"""

from __future__ import annotations

import bisect
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from agenthicc.tui.cbreak_reader import Key, MouseEvent, key_name
from agenthicc.tui.theme import current_theme, glyphs, key_arrows, palette, style
from agenthicc.tui.transcript.log import SearchJob
from agenthicc.tui.transcript.render import path_label
from agenthicc.tui.transcript.sources import TranscriptSource
from agenthicc.tui.transcript.text import highlight

if TYPE_CHECKING:
    from agenthicc.tui.keymap import Keymap
    from agenthicc.tui.workspace.live_region import Line

__all__ = ["Outcome", "OutcomeKind", "TranscriptView", "to_lines"]

#: Rows of chrome above and below the items: title, rule, rule, footer.
CHROME_ROWS = 4
#: Rendered item bodies kept.
_BODY_CACHE = 768
#: Rows the wheel scrolls.
_WHEEL_ROWS = 3
#: Seconds a notice stays in the footer.
_NOTICE_S = 4.0


class OutcomeKind(Enum):
    """What the host must do after a key."""

    NONE = auto()
    CLOSE = auto()
    COPY = auto()
    PAGER = auto()
    REWIND = auto()


@dataclass(frozen=True)
class Outcome:
    kind: OutcomeKind = OutcomeKind.NONE
    #: The text to copy or page (COPY, PAGER), or the turn's prompt (REWIND).
    text: str = ""
    #: The item acted on (REWIND).
    item: int = -1
    #: The user turn's number (REWIND).
    turn: int = 0


_NONE = Outcome()


def to_lines(console: Console, rows: Sequence[Text], width: int) -> list[Line]:
    """Rows as the segment lines a :class:`LiveRegion` draws, cropped to *width*."""
    lines: list[Line] = []
    for row in rows:
        segments = list(row.render(console, end=""))
        lines.append(tuple(Segment.adjust_line_length(segments, width, pad=False)))
    return lines


class TranscriptView:
    """Navigate a :class:`TranscriptSource`."""

    def __init__(
        self,
        source: TranscriptSource,
        keymap: Keymap,
        *,
        rewind: bool = False,
        subtitle: str = "",
        start: str = "end",
        path: str = "",
    ) -> None:
        from agenthicc.tui.keymap import Dispatcher

        self.source = source
        self.keymap = keymap
        self.rewind = rewind
        self.subtitle = subtitle
        #: A file the view shows (the ``/logs`` title ends with it).
        self.path = path
        self._dispatcher = Dispatcher(keymap)
        self.selected = -1
        self.top = 0
        #: Rows of the top item that are scrolled off.
        self.top_skip = 0
        self.expanded: set[int] = set()
        self.width = 80
        self.height = 24
        # search
        self.query = ""
        self.job: SearchJob | None = None
        self._hit = -1
        self._jump_when_done = False
        self.entering = False
        self._input: list[str] = []
        self._cursor = 0
        # state shown in the footer
        self.notice = ""
        self._notice_until = 0.0
        self._bodies: OrderedDict[tuple[object, ...], list[Text]] = OrderedDict()
        self._placed = False
        self._start = start
        #: :meth:`_signature` as of the last :meth:`render`.
        self._drawn: tuple[object, ...] = ()

    # ── geometry ─────────────────────────────────────────────────────────────

    @property
    def gutter(self) -> int:
        """Columns left of the items: the selection bar, and the turn number."""
        return 5 if self.source.has_turns else 2

    @property
    def content_rows(self) -> int:
        return max(1, self.height - CHROME_ROWS)

    def _room(self) -> int:
        return max(10, self.width - self.gutter)

    def _body(self, item: int) -> list[Text]:
        key = (item, self._room(), item in self.expanded, self.query, current_theme())
        cached = self._bodies.get(key)
        if cached is not None:
            self._bodies.move_to_end(key)
            return cached
        rows = [row.copy() for row in self.source.rows(item, self._room(), item in self.expanded)]
        if self.query:
            rows = [highlight(row, self.query) for row in rows]
        self._bodies[key] = rows
        if len(self._bodies) > _BODY_CACHE:
            self._bodies.popitem(last=False)
        return rows

    def _height(self, item: int) -> int:
        return len(self._body(item))

    def _invalidate(self) -> None:
        self._bodies.clear()

    # ── selection and anchor ─────────────────────────────────────────────────

    def _count(self) -> int:
        return self.source.count()

    def _selectable(self, item: int) -> bool:
        if self.rewind:
            turns = self.source.turn_items()
            at = bisect.bisect_left(turns, item)
            return at < len(turns) and turns[at] == item
        return self._height(item) > 0

    def _step(self, start: int, delta: int) -> int:
        """The next selectable item from *start* in the direction of *delta*, or -1."""
        item = start + delta
        count = self._count()
        while 0 <= item < count:
            if self._selectable(item):
                return item
            item += delta
        return -1

    def _anchor_bottom(self, item: int) -> None:
        """Scroll so that *item*'s last row is the last row shown (its start when taller)."""
        budget = self.content_rows
        used = 0
        at = item
        while at >= 0:
            height = self._height(at)
            if used + height >= budget:
                self.top = at
                self.top_skip = (
                    used + height - budget
                )  # an item taller than the screen shows its end
                return
            used += height
            at -= 1
        self.top, self.top_skip = 0, 0

    def _place(self, item: int, above: int) -> None:
        """Scroll so that *item* has about *above* rows over it."""
        used = 0
        at = item
        while at > 0 and used < above:
            at -= 1
            used += self._height(at)
            if used > above:
                self.top, self.top_skip = at, used - above
                return
        self.top, self.top_skip = at, 0

    def _show(self, item: int) -> None:
        """The least scrolling that shows all of *item* (its start when it is taller)."""
        if item < self.top or (item == self.top and self.top_skip > 0):
            self.top, self.top_skip = item, 0
            return
        used = -self.top_skip
        at = self.top
        while at <= item and used <= self.content_rows:
            used += self._height(at)
            at += 1
        if used > self.content_rows:
            if self._height(item) >= self.content_rows:
                self.top, self.top_skip = item, 0
            else:
                self._anchor_bottom(item)

    def _clamp(self) -> None:
        count = self._count()
        if count == 0:
            self.selected, self.top, self.top_skip = -1, 0, 0
            return
        self.selected = min(self.selected, count - 1)
        self.top = max(0, min(self.top, count - 1))
        self.top_skip = max(0, min(self.top_skip, max(0, self._height(self.top) - 1)))

    def _first_placement(self) -> None:
        """Where the view starts: the end (the newest item), or the first turn."""
        count = self._count()
        if count == 0 or self._placed or not self.source.ready:
            return
        self._placed = True
        last = self._step(count, -1)
        tool = self.source.last_tool() if self._start == "tool" else -1
        if self._start == "start":
            self.selected = max(0, self._step(-1, 1))
            self.top, self.top_skip = 0, 0
        elif tool >= 0:
            self.select(tool, expand=True, align="third")
        elif last >= 0:
            self.selected = last
            self._anchor_bottom(last)

    def select(self, item: int, *, expand: bool = False, align: str = "show") -> None:
        """Select *item* and scroll to it (``align``: ``show``, ``top`` or ``third``)."""
        count = self._count()
        if count == 0:
            return
        self.selected = max(0, min(count - 1, item))
        if expand and self.source.can_expand(self.selected):
            self.expanded.add(self.selected)
        if align == "top":
            self.top, self.top_skip = self.selected, 0
        elif align == "third":
            self._place(self.selected, self.content_rows // 3)
        else:
            self._show(self.selected)
        self._clamp()

    def _move(self, delta: int) -> None:
        count = self._count()
        if count == 0:
            return
        target = self._step(
            self.selected if self.selected >= 0 else (-1 if delta > 0 else count), delta
        )
        if target >= 0:
            self.select(target)

    def _scroll_rows(self, rows: int) -> None:
        """Move the anchor by *rows* (negative is up); the selection stays on screen."""
        count = self._count()
        if count == 0:
            return
        old_top = self.top
        if rows > 0:
            while rows > 0 and self.top < count:
                left = self._height(self.top) - self.top_skip
                if rows < left:
                    self.top_skip += rows
                    rows = 0
                elif self.top == count - 1:
                    self.top_skip = max(0, self._height(self.top) - 1)
                    rows = 0
                else:
                    rows -= left
                    self.top += 1
                    self.top_skip = 0
        else:
            rows = -rows
            while rows > 0:
                if self.top_skip >= rows:
                    self.top_skip -= rows
                    rows = 0
                elif self.top == 0:
                    self.top_skip = 0
                    rows = 0
                else:
                    rows -= self.top_skip
                    self.top -= 1
                    self.top_skip = self._height(self.top)
        if self.selected >= 0:
            moved = self.top - old_top
            target = max(0, min(count - 1, self.selected + moved))
            landing = target if self._selectable(target) else self._step(target, 1)
            if landing < 0:
                landing = self._step(target, -1)
            if landing >= 0:
                self.selected = landing

    # ── keys ─────────────────────────────────────────────────────────────────

    def conditions(self) -> frozenset[str]:
        flags: set[str] = set()
        if self.source.has_turns:
            flags.add("turns")
        if self.rewind:
            flags.add("rewind")
        elif self.selected >= 0 and self.source.can_expand(self.selected):
            flags.add("expandable")
        if self.job is not None and self.job.hits:
            flags.add("hits")
        return frozenset(flags)

    def handle_key(self, key: Key, ch: str) -> Outcome:
        """Apply one keystroke; say what the host has to do."""
        if self.entering:
            return self._entering_key(key, ch)
        if key is Key.MOUSE:
            event = MouseEvent.decode(ch)
            if event is not None and event.wheel:
                self._scroll_rows(event.wheel * _WHEEL_ROWS)
            return _NONE
        self._first_placement()
        action = self._dispatcher.dispatch("transcript_view", key_name(key, ch), self.conditions())
        if action is None:
            return _NONE
        return self._act(action.name)

    def _act(self, name: str) -> Outcome:
        count = self._count()
        match name:
            case "transcript_view.close":
                self.source.cancel_search()
                return Outcome(OutcomeKind.CLOSE)
            case "transcript_view.up":
                self._move(-1)
            case "transcript_view.down":
                self._move(1)
            case "transcript_view.page_up":
                self._scroll_rows(-max(1, self.content_rows - 1))
            case "transcript_view.page_down":
                self._scroll_rows(max(1, self.content_rows - 1))
            case "transcript_view.top" if count:
                first = max(0, self._step(-1, 1))
                self.selected, self.top, self.top_skip = first, 0, 0
            case "transcript_view.bottom" if count:
                last = self._step(count, -1)
                self.selected = max(0, last)
                self._anchor_bottom(count - 1)
            case "transcript_view.turn_previous":
                self._jump(self.source.turn_items(), -1, "turn")
            case "transcript_view.turn_next":
                self._jump(self.source.turn_items(), 1, "turn")
            case "transcript_view.attention_previous":
                self._jump(self.source.attention_items(), -1, "failure")
            case "transcript_view.attention_next":
                self._jump(self.source.attention_items(), 1, "failure")
            case "transcript_view.expand":
                self._toggle(self.selected)
            case "transcript_view.rewind":
                return self._rewind()
            case "transcript_view.search":
                self.entering = True
                self._input = list(self.query)
                self._cursor = len(self._input)
            case "transcript_view.search_next":
                self._hit_step(1)
            case "transcript_view.search_previous":
                self._hit_step(-1)
            case "transcript_view.copy" if self.selected >= 0:
                return Outcome(
                    OutcomeKind.COPY, self.source.copy_text(self.selected), self.selected
                )
            case "transcript_view.editor" if self.selected >= 0:
                return Outcome(
                    OutcomeKind.PAGER, self.source.copy_text(self.selected), self.selected
                )
        return _NONE

    def _toggle(self, item: int) -> None:
        if item < 0 or not self.source.can_expand(item):
            return
        self.expanded ^= {item}
        self._show(item)
        self._clamp()

    def _jump(self, items: Sequence[int], direction: int, word: str) -> None:
        """Select the nearest of *items* past the selection (a turn or a failure)."""
        if not items:
            self.say(f"no {word}s here")
            return
        if direction > 0:
            at = bisect.bisect_right(items, self.selected)
            target = items[at] if at < len(items) else -1
        else:
            at = bisect.bisect_left(items, self.selected) - 1
            target = items[at] if at >= 0 else -1
        if target < 0:
            self.say(f"no {'later' if direction > 0 else 'earlier'} {word}")
            return
        self.select(target, align="top" if word == "turn" else "third")

    def _rewind(self) -> Outcome:
        item = self.selected
        if item < 0:
            return _NONE
        return Outcome(
            OutcomeKind.REWIND, self.source.user_text(item), item, self.source.turn_number(item)
        )

    # ── search ───────────────────────────────────────────────────────────────

    def _entering_key(self, key: Key, ch: str) -> Outcome:
        match key:
            case Key.ESC:
                self.entering = False
            case Key.ENTER:
                self.entering = False
                self._search("".join(self._input))
            case Key.BACKSPACE:
                if self._cursor:
                    del self._input[self._cursor - 1]
                    self._cursor -= 1
            case Key.LEFT:
                self._cursor = max(0, self._cursor - 1)
            case Key.RIGHT:
                self._cursor = min(len(self._input), self._cursor + 1)
            case Key.HOME | Key.CTRL_A:
                self._cursor = 0
            case Key.END | Key.CTRL_E:
                self._cursor = len(self._input)
            case Key.CTRL_U:
                self._input, self._cursor = [], 0
            case Key.CHAR if ch and ch.isprintable():
                self._input.insert(self._cursor, ch)
                self._cursor += 1
            case Key.PASTE:
                pasted = [c for c in ch if c.isprintable()]
                self._input[self._cursor : self._cursor] = pasted
                self._cursor += len(pasted)
        return _NONE

    def _search(self, query: str) -> None:
        self.source.cancel_search()
        self.query = query
        self._invalidate()
        self._hit = -1
        if not query:
            self.job = None
            return
        self.job = self.source.start_search(query)
        self._jump_when_done = True
        self.poll()

    def poll(self) -> None:
        """Take what a worker finished: the first hit once a search is done."""
        job = self.job
        if job is None or not job.done or not self._jump_when_done:
            return
        self._jump_when_done = False
        if not job.hits:
            self.say(f"no matches for {job.query!r}")
            return
        after = bisect.bisect_right(job.hits, self.selected)
        self._hit = after if after < len(job.hits) else 0
        self._goto_hit()

    def _hit_step(self, direction: int) -> None:
        job = self.job
        if job is None or not job.hits:
            return
        self._hit = (self._hit + direction) % len(job.hits) if self._hit >= 0 else 0
        self._goto_hit()

    def _goto_hit(self) -> None:
        job = self.job
        if job is None or not 0 <= self._hit < len(job.hits):
            return
        self.select(job.hits[self._hit], align="third")

    # ── notices ──────────────────────────────────────────────────────────────

    def say(self, text: str) -> None:
        """Show *text* in the footer for a few seconds."""
        self.notice = text
        self._notice_until = time.monotonic() + _NOTICE_S

    # ── following a log that grows ───────────────────────────────────────────

    def _signature(self) -> tuple[object, ...]:
        """What the screen shows that is not in a key: compared with the last frame."""
        return (
            self.selected,
            self.top,
            self.top_skip,
            self._status(),
            self.source.ready,
            self._count(),
            self.entering,
        )

    def tick(self) -> bool:
        """Pick up appended items and finished work; whether the screen is out of date."""
        before = self._count()
        at_end = self.selected < 0 or self.selected >= before - 1
        added = self.source.refresh()
        self.poll()
        if added and at_end:
            self._placed = True
            last = self._step(self._count(), -1)
            if last >= 0:
                self.selected = last
                self._anchor_bottom(self._count() - 1)
        return self._signature() != self._drawn

    # ── drawing ──────────────────────────────────────────────────────────────

    def render(self, width: int, height: int) -> list[Text]:
        """Exactly *height* rows of *width* cells: chrome and the items on screen."""
        self.width, self.height = max(20, width), max(CHROME_ROWS + 1, height)
        self.source.fit(self._room())
        self.poll()
        self._first_placement()
        self._clamp()
        p, g = palette(), glyphs()
        rule = Text(g.rule * self.width, style=p.subtle, no_wrap=True)
        rows = [self._title_row(), rule]
        rows += self._content()
        rows += [rule.copy(), self._footer_row()]
        self._drawn = self._signature()
        return rows[: self.height]

    def _title_row(self) -> Text:
        p, g = palette(), glyphs()
        count = len(self.source.turn_items())
        parts = ["Transcript"] if self.source.has_turns else ["Log"]
        if self.subtitle:
            parts.append(self.subtitle)
        if self.source.has_turns and self.source.ready:
            parts.append(f"{count} turn{'s' if count != 1 else ''}")
        close = self.keymap.hint("transcript_view.close")
        left = Text(f" {f' {g.sep} '.join(parts)}", style=style("accent", bold=True), no_wrap=True)
        if self.path:
            left.append(f" {g.sep} ", p.muted)
            left.append_text(path_label(self.path))
        right = Text(f"{close.keys if close else 'q'} close ", style=p.muted)
        pad = self.width - left.cell_len - right.cell_len
        if pad < 1:
            left.truncate(max(1, self.width - right.cell_len - 1), overflow="ellipsis")
            pad = max(1, self.width - left.cell_len - right.cell_len)
        left.append(" " * pad)
        left.append_text(right)
        return left

    def _content(self) -> list[Text]:
        rows: list[Text] = []
        p = palette()
        budget = self.content_rows
        if not self.source.ready:
            rows.append(Text(" Reading the log" + glyphs().ellipsis, style=p.muted))
        elif self._count() == 0:
            rows.append(Text(" Nothing to show yet.", style=p.muted))
        else:
            item, skip = self.top, self.top_skip
            count = self._count()
            while len(rows) < budget and item < count:
                body = self._body(item)
                for index in range(skip, len(body)):
                    if len(rows) >= budget:
                        break
                    rows.append(self._compose(item, index, body[index]))
                item, skip = item + 1, 0
        while len(rows) < budget:
            rows.append(Text(""))
        return rows

    def _compose(self, item: int, index: int, body: Text) -> Text:
        """One screen row: the gutter, then the item's row (accented when selected)."""
        p, g = palette(), glyphs()
        selected = item == self.selected
        row = Text(no_wrap=True, overflow="crop")
        row.append(g.bar if selected else " ", p.accent)
        if self.source.has_turns:
            number = self.source.turn_number(item) if index == 0 and self._is_turn(item) else 0
            row.append(f"{number:>3} " if number else "    ", p.muted)
        else:
            row.append(" ")
        row.append_text(body)
        # The tag sits on the first output row (the header row is full).
        if item in self.expanded and index == min(1, self._height(item) - 1):
            tag = "[expanded]"
            # The renderers pad a wrapped row to the width: the tag replaces the
            # padding, unless that padding carries a style (a user message's band).
            used = len(row.plain.rstrip())
            plain_tail = not any(span.end > used for span in row.spans)
            room = self.width - used
            if plain_tail and room > len(tag) + 2:
                row.right_crop(len(row.plain) - used)
                row.append(" " * (room - len(tag) - 1))
                row.append(tag, p.muted)
        if selected:
            row.pad_right(max(0, self.width - row.cell_len))
            row.stylize(Style.parse(p.on("accent_bg")))
        return row

    def _is_turn(self, item: int) -> bool:
        turns = self.source.turn_items()
        at = bisect.bisect_left(turns, item)
        return at < len(turns) and turns[at] == item

    def _footer_row(self) -> Text:
        p, g = palette(), glyphs()
        if self.entering:
            row = Text(" /", style=style("accent", bold=True), no_wrap=True)
            row.append(" ")
            typed = "".join(self._input)
            row.append(typed[: self._cursor])
            row.append(typed[self._cursor] if self._cursor < len(typed) else " ", "reverse")
            row.append(typed[self._cursor + 1 :])
            return row
        right = self._status()
        hints = self.keymap.hints("transcript_view", self.conditions())
        left = Text(" ", no_wrap=True)
        room = self.width - len(right) - 2
        for hint in hints:
            piece = f"{key_arrows(hint.keys)} {hint.label}"
            sep = f" {g.sep} " if left.cell_len > 1 else ""
            if left.cell_len + len(sep) + len(piece) > room:
                break
            left.append(sep, p.subtle)
            left.append(piece, p.muted)
        if right:
            left.append(" " * max(1, self.width - left.cell_len - len(right) - 1))
            left.append(right, p.muted)
        return left

    def _status(self) -> str:
        job = self.job
        if self.notice and time.monotonic() < self._notice_until:
            return self.notice
        if job is not None and not job.done:
            return f"searching{glyphs().ellipsis}"
        if job is not None and job.hits and self._hit >= 0:
            return f"hit {self._hit + 1} of {len(job.hits)}"
        if job is not None and job.done and not job.hits:
            return "no matches"
        return ""

    # ── helpers for hosts ────────────────────────────────────────────────────

    @property
    def selected_text(self) -> str:
        return self.source.copy_text(self.selected) if self.selected >= 0 else ""
