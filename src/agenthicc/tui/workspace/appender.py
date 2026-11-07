"""ScrollBufferAppender — the ONLY component allowed to call console.print().

Every ConversationEvent is rendered exactly once to stdout (above the always-on
Live region).  Rich's Console.print() while a Live block is active automatically
inserts content above the Live block.

The transcript is a narrative, not a log (TUX-06): one quiet row per tool
call, the real command and its result in plain words, failures expanded, and
more detail on demand (``[ui] tool_detail`` and ``/details``).  Events carry
data; every colour and glyph comes from :mod:`agenthicc.tui.theme` at render
time, so a theme change restyles replayed history too.

Architecture: PRD-60 §7, PRD-66 §3, TUX-06.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Final

from agenthicc.tui.conversation_store import ConversationEvent
from agenthicc.tui.theme import glyphs, palette, style
from agenthicc.tui.tool_summary import (
    ToolFamily,
    middle_truncate,
    test_summary,
    tool_presentation,
)
from agenthicc.tui.workspace.formatters import (
    compact_duration,
    compact_token_count,
    cost_label,
    identity_rows,
    usage_quality_label,
)

# ── event renderer registry ───────────────────────────────────────────────────
# Maps ConversationEvent.kind → bound-method-compatible handler.
# Register new event kinds by calling register_renderer() from any module that
# defines a new ConversationEvent kind — no edits to _render_one() needed.
_EventRenderer = Callable[["ScrollBufferAppender", ConversationEvent], None]
_RENDERERS: dict[str, _EventRenderer] = {}


def register_renderer(kind: str) -> Callable[[_EventRenderer], _EventRenderer]:
    """Decorator: register a function as the renderer for *kind* events."""

    def decorator(fn: _EventRenderer) -> _EventRenderer:
        _RENDERERS[kind] = fn
        return fn

    return decorator


if TYPE_CHECKING:
    from rich.console import Console
    from rich.style import Style
    from rich.text import Text

    from agenthicc.tui.conversation_store import AppState


# ── tool detail level (TUX-06 §2.4) ───────────────────────────────────────────

#: ``compact`` (default): one row per call; ``normal``: plus a four-line head
#: preview for reads and commands; ``verbose``: the whole bounded output.
TOOL_DETAIL_LEVELS: Final = ("compact", "normal", "verbose")

_session_detail: str | None = None


def session_tool_detail() -> str | None:
    """The level ``/details`` chose for this session, or ``None`` (use config)."""
    return _session_detail


def set_session_tool_detail(level: str | None) -> None:
    """Choose the tool detail level for the rest of the session.

    Rows already printed are never re-rendered: the scrollback stays an
    append-only log (AGX-11).  ``None`` returns to ``[ui] tool_detail``.
    """
    global _session_detail
    if level is not None and level not in TOOL_DETAIL_LEVELS:
        raise ValueError(f"tool detail must be one of: {', '.join(TOOL_DETAIL_LEVELS)}")
    _session_detail = level


_LANG_MAP: dict[str, str] = {
    "py": "python",
    "js": "javascript",
    "ts": "typescript",
    "jsx": "jsx",
    "tsx": "tsx",
    "json": "json",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "toml",
    "md": "markdown",
    "sh": "bash",
    "rs": "rust",
    "go": "go",
    "rb": "ruby",
    "c": "c",
    "cpp": "cpp",
    "h": "c",
    "cs": "csharp",
    "java": "java",
    "kt": "kotlin",
    "swift": "swift",
    "html": "html",
    "css": "css",
    "sql": "sql",
}

_MAX_EXPLORATION_ITEMS = 12
_MAX_GOAL_DISPLAY_COUNT = 1_000_000
#: Output lines a failed call shows, from the end of its output.
_FAILURE_TAIL_LINES = 8
#: Head lines the ``normal`` level previews for reads and commands.
_PREVIEW_LINES = 4
#: Changed rows an edit shows below its header unless the level is verbose.
_DIFF_ROWS = 20
#: A turn header is printed again after this much quiet time.
_HEADER_GAP_S = 30 * 60
_EXPAND_HINT = "ctrl+t to expand"

_LIVE_WORDS: dict[str, tuple[str, str]] = {
    "ready": ("ready", "info"),
    "running": ("running", "info"),
    "waiting": ("waiting", "warning"),
}
_FAILURE_WORDS: dict[str, str] = {
    "timed_out": "timed out",
    "cancelled": "cancelled",
    "starting_timeout": "readiness timeout",
    "spawn_failed": "could not start",
    "rejected": "rejected",
    "orphaned": "orphaned",
}
_PATH_FAMILIES = frozenset({ToolFamily.READ, ToolFamily.WRITE, ToolFamily.LIST})


def _lang_for_path(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return _LANG_MAP.get(ext, "text")


def _hhmm(ts: float) -> str:
    """Local wall-clock time of *ts*: ``HH:MM`` (the zone is in ``/status``)."""
    return time.strftime("%H:%M", time.localtime(ts))


def _text(payload: Mapping[str, object], key: str, default: str = "") -> str:
    value = payload.get(key, default)
    return value if isinstance(value, str) else str(value)


def _number(payload: Mapping[str, object], key: str, default: float = 0.0) -> float:
    value = payload.get(key, default)
    return (
        float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    )


def _nonnegative_int(payload: Mapping[str, object], key: str, default: int = 0) -> int:
    value = payload.get(key, default)
    return (
        value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default
    )


def _optional_int(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _duration_label(ms: int | None) -> str:
    """``2.4s`` / ``1m 1s`` for calls of a second or more; quicker ones say nothing."""
    if ms is None or ms < 1_000:
        return ""
    seconds = ms / 1_000
    return f"{seconds:.1f}s" if seconds < 60 else compact_duration(seconds)


def _plural(count: int, word: str, plural: str = "") -> str:
    return f"{count:,} {word if count == 1 else plural or word + 's'}"


@dataclass(frozen=True, slots=True)
class _ToolView:
    """The fields of one ``tool_complete`` payload the renderer reads."""

    name: str
    operation: str
    family: ToolFamily
    summary: str
    detail: str
    lines: tuple[str, ...]
    head: int
    total: int
    duration_ms: int | None
    exit_code: int | None
    error_code: str | None
    live_word: str
    live_token: str
    failure_word: str
    failed: bool

    @classmethod
    def of(cls, payload: Mapping[str, object]) -> _ToolView:
        name = _text(payload, "name")
        presentation = tool_presentation(name)
        lines = tuple(_string_list(payload.get("output_lines")))
        head = min(_nonnegative_int(payload, "output_head", len(lines)), len(lines))
        total = max(_nonnegative_int(payload, "output_total", len(lines)), len(lines))
        state = payload.get("state")
        exit_code = _optional_int(payload, "exit_code")
        raw_error = payload.get("error_code")
        error_code = raw_error if isinstance(raw_error, str) and raw_error else None
        live_word, live_token = "", ""
        if state == "running":
            live_word, live_token = _LIVE_WORDS[
                "ready" if payload.get("ready") is True else "running"
            ]
        elif state == "waiting":
            live_word, live_token = _LIVE_WORDS["waiting"]
        failure_word = _FAILURE_WORDS.get(state, "") if isinstance(state, str) else ""
        failed = not live_word and (
            payload.get("success", True) is False
            or state not in (None, "exited")
            or (exit_code is not None and exit_code != 0)
            or error_code is not None
        )
        duration = _optional_int(payload, "duration_ms")
        return cls(
            name=name,
            operation=presentation.operation,
            family=presentation.family,
            summary=_text(payload, "summary"),
            detail=_text(payload, "summary_detail"),
            lines=lines,
            head=head,
            total=total,
            duration_ms=duration,
            exit_code=exit_code,
            error_code=error_code,
            live_word=live_word,
            live_token=live_token,
            failure_word=failure_word,
            failed=failed,
        )

    @property
    def tail_known(self) -> bool:
        """Whether ``lines`` ends with the real last line of the output."""
        return self.head < len(self.lines) or len(self.lines) == self.total

    def tail(self, count: int) -> tuple[str, ...]:
        return self.lines[-count:] if count > 0 else ()

    def result_line(self) -> str:
        """The command's one-line result: a test summary or its last line."""
        tested = test_summary(
            self.lines[self.head :] if self.head < len(self.lines) else self.lines
        )
        if tested:
            return tested
        if not self.tail_known:
            return ""
        return next((line.strip() for line in reversed(self.lines) if line.strip()), "")


@dataclass(slots=True)
class _ExplorationItem:
    operation: str
    target: str
    files: int


def _goal_mutation_lines(payload: Mapping[str, object]) -> tuple[str, str, str]:
    """Return bounded, human-facing lines for a committed goal mutation.

    ``insert_goal`` stores a zero-based ``index`` because that is the stable
    workflow/tool contract.  The TUI deliberately translates it to a
    one-based position and only displays it when the event contains coherent,
    bounded values.  This helper is presentation-only: it never reads or
    modifies workflow state and never renders the opaque goal ID or goal text.
    """

    operation = payload.get("operation")
    operation_name = operation if isinstance(operation, str) else ""
    raw_index = payload.get("index")
    raw_count = payload.get("goal_count")
    index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else None
    count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else None
    bounded_count = count is not None and 0 < count <= _MAX_GOAL_DISPLAY_COUNT
    bounded_index = index is not None and 0 <= index <= _MAX_GOAL_DISPLAY_COUNT

    position: int | None = None
    total: int | None = None
    if bounded_count and bounded_index:
        assert count is not None
        assert index is not None
        candidate_count = count
        candidate_index = index
        # The event is emitted after the mutation.  Therefore the new record
        # must occupy [0, goal_count), and an append must occupy the final
        # position.  Do not display contradictory historical/malformed data.
        operation_is_coherent = 0 <= candidate_index < candidate_count and (
            operation_name != "append" or candidate_index == candidate_count - 1
        )
        if operation_is_coherent:
            position = candidate_index + 1
            total = candidate_count

    if operation_name == "insert":
        detail = "Inserted a goal"
    elif operation_name == "append":
        detail = "Added a goal to the end"
    else:
        detail = "Added a goal"

    if position is not None and total is not None:
        detail += f" · position {position} of {total}"
    elif bounded_count:
        assert count is not None
        detail += f" · plan now has {count} goal{'s' if count != 1 else ''}"

    return ("Plan updated", detail, "↳ Current goal continues")


#: Event kinds that render as tool rows: consecutive ones print no blank between.
_TOOL_KINDS: Final = frozenset({"tool_complete", "file_modified"})
#: Kinds whose renderer places its own blank line and pending turn header.
_SELF_SPACED_KINDS: Final = _TOOL_KINDS | {"mention_chips", "turn_start"}
#: Presentation-only kinds whose timestamp is "now", not the conversation's.
_UNTIMED_KINDS: Final = frozenset({"announcement", "replay_separator"})


class ScrollBufferAppender:
    """Subscribes to ConversationStore events and renders them to stdout.

    Instantiate once, call mount() after the Live block is started.
    All rendering is queued via call_soon_threadsafe to ensure it happens
    on the asyncio event-loop thread (not on background threads).
    """

    def __init__(
        self,
        app_state: AppState,
        console: Console,
        max_live_tool_calls: int = 5,
        group_exploratory_calls: bool = True,
    ) -> None:
        self._state: AppState = app_state
        self._console: Console = console
        self._unsub: Callable[[], None] | None = None
        self._max_tool_calls: int = max_live_tool_calls
        self._group_exploratory_calls: bool = group_exploratory_calls
        # Small batch buffer to coalesce rapid events (e.g. many tool completions)
        self._pending: list[ConversationEvent] = []
        self._flush_scheduled = False
        # Tracks how many tool_complete events have been seen in the current
        # consecutive group, and how many of them were not printed.  Failed
        # calls always print; the rest stop at ``max_live_tool_calls``.
        self._group_count: int = 0
        self._group_hidden: int = 0
        # Contiguous exploratory calls collapse to one row printed when the
        # group closes (TUX-06); the store still holds one event per call.
        self._exploration: list[_ExplorationItem] = []
        # Rhythm: a blank line is owed before the next block that is not a
        # continuation (a tool row after a tool row, chips after the user).
        self._gap_pending: bool = False
        self._last_block: str = ""
        # The agent and model of the previous turn, for the header rule.
        self._turn_identity: tuple[str, str] | None = None
        self._last_event_ts: float | None = None
        # A turn header waits for the mention chips that belong to the user.
        self._pending_header: Text | None = None
        # ``/details`` lasts for the session that owns this appender.
        set_session_tool_detail(None)

    def mount(self) -> None:
        self._unsub = self._state.conversation.on_event(self._queue_event)

    def unmount(self) -> None:
        # Shutdown can race the final cancellation/turn event.  Drain queued
        # events and close both kinds of tool group so the live overflow row
        # cannot be left behind as the terminal is torn down.
        self.flush()
        if self._unsub:
            self._unsub()
            self._unsub = None

    def flush(self) -> None:
        """Render queued events and persist any open tool-group summary.

        Interrupts cancel the agent task before its normal ``turn_complete``
        event necessarily reaches the appender.  The workspace calls this
        method at that boundary so the collapsed tool count is printed in the
        scroll buffer instead of remaining in the live footer.  It is safe to
        call repeatedly; both group counters are reset after flushing.
        """
        if self._pending or self._flush_scheduled:
            self._flush_batch()
        with self._console:
            self._flush_exploration_group()
            self._flush_group_summary()
            self._print_pending_header()

    def replay(
        self,
        events: Iterable[ConversationEvent],
        *,
        continue_group: bool = False,
    ) -> None:
        """Queue persisted events for display without persisting them again.

        Resume must restore the visible transcript, but feeding historical
        events through ``ConversationStore.append_event`` would notify the
        session-log subscriber and duplicate the JSONL records. The appender
        owns this presentation-only path and renders the original event
        timestamps/order through the same renderers as live events.
        """
        if not continue_group:
            self._flush_exploration_group()
        for event in events:
            event.rendered = False
            self._queue_event(event)

    def replay_boundary(self, *, since: float | None = None, events: int = 0) -> None:
        """Mark where replayed history starts (*since* given) or ends (TUX-06 §2.6).

        ``── replayed from 2 Nov 05:04 · 827 events ──`` opens the history and
        ``── now ──`` closes it; like :meth:`replay`, presentation only.
        """
        payload: dict[str, object] = (
            {"edge": "start", "since": since, "events": events}
            if since is not None
            else {"edge": "end"}
        )
        self._queue_event(ConversationEvent(event_id="", kind="replay_separator", payload=payload))

    def announce(self, text: str) -> None:
        """Print a plain sentence in order with the transcript (AGX-13).

        Used for screen-reader announcements; like :meth:`replay`, it is
        presentation only and never reaches the session's event log.
        """
        self._queue_event(
            ConversationEvent(event_id="", kind="announcement", payload={"text": text})
        )

    # ── event queuing ─────────────────────────────────────────────────────────

    def _queue_event(self, ev: ConversationEvent) -> None:
        self._pending.append(ev)
        if not self._flush_scheduled:
            self._flush_scheduled = True
            try:
                asyncio.get_running_loop().call_soon(self._flush_batch)
            except RuntimeError:
                self._flush_batch()

    def _flush_batch(self) -> None:
        self._flush_scheduled = False
        pending, self._pending = self._pending, []
        # Wrap the entire batch in one console context so all console.print()
        # calls share a single write() + flush().  Without this, each print
        # triggers its own erase-Live-Block → write-content → redraw-Live-Block
        # cycle, causing the status bar to flicker once per event.
        with self._console:
            for ev in pending:
                if not ev.rendered:
                    ev.rendered = True
                    try:
                        self._render_one(ev)
                    except Exception:  # noqa: BLE001
                        pass

    # ── rendering ─────────────────────────────────────────────────────────────

    def _render_one(self, ev: ConversationEvent) -> None:
        """Dispatch to the registered renderer for ev.kind; silently ignore unknown kinds."""
        renderer = _RENDERERS.get(ev.kind)
        if renderer is None:
            # Invisible kinds (tokens, cache diagnostics) never split a group.
            return
        if ev.kind != "tool_complete":
            self._flush_exploration_group()
            # Close regular tool groups before any boundary event, so the
            # live footer's ``...and N more`` never sits below the next block.
            self._flush_group_summary()
        if ev.kind not in _SELF_SPACED_KINDS:
            self._print_pending_header()
            self._open_block(ev.kind)
        renderer(self, ev)
        if ev.kind not in _UNTIMED_KINDS:
            self._last_event_ts = ev.timestamp

    def _open_block(self, kind: str) -> None:
        """Print the owed blank line before a new block, unless it continues one."""
        continuing = kind in _TOOL_KINDS and self._last_block == "tool"
        if self._gap_pending and not continuing:
            self._console.print()
        self._gap_pending = False
        self._last_block = "tool" if kind in _TOOL_KINDS else kind

    def _print_pending_header(self) -> None:
        header, self._pending_header = self._pending_header, None
        if header is not None:
            self._open_block("turn_header")
            self._console.print(header, highlight=False, soft_wrap=True)

    def _tool_detail(self) -> str:
        if _session_detail is not None:
            return _session_detail
        from agenthicc.tui.preferences import ui_settings

        ui = ui_settings(self._state)
        return ui.tool_detail if ui is not None else "compact"

    # ── tool rows ─────────────────────────────────────────────────────────────

    def _tool_row(
        self,
        marker: str,
        marker_style: str | Style,
        operation: str,
        target: str,
        detail: str,
        meta: Text | None,
        *,
        path: bool,
    ) -> None:
        """One width-fitted row: ``● Operation target  detail      meta``."""
        from rich.cells import cell_len
        from rich.text import Text

        p = palette()
        width = max(20, self._console.width)
        meta_width = meta.cell_len + 2 if meta is not None and meta.cell_len else 0
        fixed = cell_len(f"{marker} {operation} ") + meta_width
        # The target keeps priority; quiet detail is cut first, then dropped.
        target_room = min(cell_len(target), max(12, width - fixed - 10))
        detail_room = width - fixed - target_room - 2
        detail_text = ""
        if detail and detail_room >= 6:
            ellipsis = glyphs().ellipsis
            detail_text = "  " + (
                detail
                if cell_len(detail) <= detail_room
                else detail[: detail_room - len(ellipsis)] + ellipsis
            )
        room = width - fixed - cell_len(detail_text)
        if cell_len(target) > room:
            target = middle_truncate(target, max(1, room), path=path, ellipsis=glyphs().ellipsis)
        row = Text.assemble(
            (marker, marker_style),
            " ",
            (operation, f"bold {p.text}"),
            " " if target else "",
            (target, p.text),
            (detail_text, p.muted),
        )
        if meta is not None and meta.cell_len:
            row.append(" " * max(2, width - row.cell_len - meta.cell_len))
            row.append_text(meta)
        row.truncate(width, overflow="ellipsis")
        self._console.print(row, highlight=False, no_wrap=True, overflow="ellipsis")

    def _child(self, glyph: str, text: str, style: str) -> None:
        from rich.text import Text

        width = max(20, self._console.width)
        row = Text.assemble((f"  {glyph} ", palette().muted), (text, style))
        row.truncate(width, overflow="ellipsis")
        self._console.print(row, highlight=False, no_wrap=True, overflow="ellipsis")

    def _render_tool_complete(self, payload: Mapping[str, object]) -> None:
        from rich.text import Text

        p, g = palette(), glyphs()
        view = _ToolView.of(payload)
        level = self._tool_detail()
        duration = _duration_label(view.duration_ms)
        meta = Text()
        marker_style: str | Style
        if view.failed:
            reason = (
                f"exit {view.exit_code}"
                if view.exit_code not in (None, 0)
                else view.failure_word or (view.error_code or "").replace("_", " ") or "failed"
            )
            meta.append(reason, p.danger)
            if duration:
                meta.append(f" {g.sep} {duration}", p.muted)
            marker, marker_style = g.fail, f"bold {p.danger}"
        elif view.live_word:
            live_style = style(view.live_token)
            meta.append(view.live_word, live_style)
            if duration:
                meta.append(f" {g.sep} {duration}", p.muted)
            marker, marker_style = g.bullet, live_style
        else:
            meta.append(duration, p.muted)
            marker, marker_style = g.bullet, p.muted
        self._tool_row(
            marker,
            marker_style,
            view.operation,
            view.summary,
            view.detail,
            meta,
            path=view.family in _PATH_FAMILIES,
        )
        if view.failed:
            self._render_failure_output(view, level)
        elif not view.live_word:
            self._render_success_output(view, level)

    def _render_failure_output(self, view: _ToolView, level: str) -> None:
        p, g = palette(), glyphs()
        if level == "verbose":
            self._render_all_lines(view)
            shown = len(view.lines)
        else:
            lines = view.tail(_FAILURE_TAIL_LINES)
            for line in lines:
                self._child(g.pipe, line, p.text)
            shown = len(lines)
        hidden = view.total - shown
        footer = (
            f"{_plural(hidden, 'more line')} {g.sep} {_EXPAND_HINT}" if hidden else _EXPAND_HINT
        )
        self._child(g.elbow, footer, p.muted)

    def _render_success_output(self, view: _ToolView, level: str) -> None:
        p, g = palette(), glyphs()
        command = view.family is ToolFamily.COMMAND
        previewed = 0
        if level == "verbose" and view.lines:
            self._render_all_lines(view)
            previewed = len(view.lines)
        elif level == "normal" and (command or view.family is ToolFamily.READ):
            for line in view.lines[: min(_PREVIEW_LINES, view.head or len(view.lines))]:
                self._child(g.pipe, line, p.text)
                previewed += 1
        result = view.result_line() if command else ""
        if result:
            self._child(g.elbow, result, p.muted)
        elif previewed and view.total > previewed:
            self._child(g.elbow, _plural(view.total - previewed, "more line"), p.muted)

    def _render_all_lines(self, view: _ToolView) -> None:
        p, g = palette(), glyphs()
        for index, line in enumerate(view.lines):
            if index == view.head and view.total > len(view.lines):
                omitted = view.total - len(view.lines)
                self._child(g.pipe, f"{g.ellipsis} {_plural(omitted, 'line')} omitted", p.muted)
            self._child(g.pipe, line, p.text)

    # ── exploration groups ───────────────────────────────────────────────────

    def _flush_exploration_group(self) -> None:
        """Print the one-row summary of the open exploratory group (TUX-06)."""
        if not self._exploration:
            return
        self._print_pending_header()
        items, self._exploration = self._exploration, []
        p, g = palette(), glyphs()
        files = sum(item.files for item in items if item.operation == "Read")
        searches = sum(1 for item in items if item.operation in {"Search", "Find"})
        listings = sum(1 for item in items if item.operation == "List")
        others = len(items) - sum(
            1 for item in items if item.operation in {"Read", "Search", "Find", "List"}
        )
        parts = [
            label
            for count, label in (
                (files, _plural(files, "file")),
                (searches, _plural(searches, "search", "searches")),
                (listings, _plural(listings, "listing")),
                (others, _plural(others, "lookup")),
            )
            if count
        ]
        targets = ", ".join(item.target for item in items if item.target)
        self._open_block("tool_complete")
        self._tool_row(g.bullet, p.muted, "Explored", ", ".join(parts), targets, None, path=False)
        if self._tool_detail() == "verbose":
            for item in items[:_MAX_EXPLORATION_ITEMS]:
                self._child(g.elbow, f"{item.operation} {item.target}".strip(), p.muted)
            hidden = len(items) - _MAX_EXPLORATION_ITEMS
            if hidden > 0:
                self._child(g.elbow, f"{g.ellipsis}and {_plural(hidden, 'more call')}", p.muted)
        self._gap_pending = True

    def _exploration_payload(self, payload: Mapping[str, object]) -> Mapping[str, object] | None:
        if not self._group_exploratory_calls:
            return None
        presentation = payload.get("presentation")
        if not isinstance(presentation, Mapping) or presentation.get("exploratory") is not True:
            return None
        return presentation

    def _add_exploration(
        self,
        payload: Mapping[str, object],
        presentation: Mapping[str, object],
    ) -> None:
        """Add one successful exploratory call to the open derived group."""
        if self._group_count:
            self._flush_group_summary()
        name = _text(payload, "name")
        operation = tool_presentation(name).operation
        files = max(1, _nonnegative_int(presentation, "file_count", 1))
        self._exploration.append(_ExplorationItem(operation, _text(payload, "summary"), files))

    def _flush_group_summary(self) -> None:
        """Close the current tool group: reset the live signal, print permanent record."""
        hidden = self._group_hidden
        self._group_count = 0
        self._group_hidden = 0
        # Always clear the live footer indicator first.
        self._state.conversation.live_tool_overflow.set(0)
        if hidden > 0:
            self._child(
                glyphs().elbow,
                f"{glyphs().ellipsis}and {_plural(hidden, 'more tool call')}",
                palette().muted,
            )

    # ── idle status header (printed once before each new prompt) ──────────────

    def print_idle_header(self) -> None:
        """Print a compact, width-bounded session summary before the prompt."""
        import shutil

        from rich.markup import escape

        from agenthicc.tui.preferences import ui_settings
        from agenthicc.tui.rendering import fit, visible_len

        p, g = palette(), glyphs()
        conv = self._state.conversation
        cols = max(1, shutil.get_terminal_size((80, 24)).columns)
        ui = ui_settings(self._state)
        reader = ui is not None and ui.screen_reader

        def signal(name: str, default: object) -> object:
            value = getattr(conv, name, default)
            return value() if callable(value) else value

        raw_sid = signal("session_id", "")
        sid = raw_sid if isinstance(raw_sid, str) else ""
        raw_directory = signal("current_directory", "")
        current_directory = raw_directory if isinstance(raw_directory, str) else ""
        raw_turns = signal("turn_count", 0)
        turns = raw_turns if isinstance(raw_turns, int) and not isinstance(raw_turns, bool) else 0
        raw_cost = signal("cost_usd", 0.0)
        cost_value = (
            float(raw_cost)
            if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool)
            else 0.0
        )
        raw_in = signal("tokens_in", 0)
        tokens_in = raw_in if isinstance(raw_in, int) and not isinstance(raw_in, bool) else 0
        raw_out = signal("tokens_out", 0)
        tokens_out = raw_out if isinstance(raw_out, int) and not isinstance(raw_out, bool) else 0
        raw_usage_status = signal("usage_status", "unavailable")
        usage_status = raw_usage_status if isinstance(raw_usage_status, str) else "unavailable"
        raw_cost_status = signal("cost_status", "unavailable")
        cost_status = raw_cost_status if isinstance(raw_cost_status, str) else "unavailable"

        if usage_status in {"complete", "partial"}:
            if reader:
                usage = f"{tokens_in:,} input tokens, {tokens_out:,} output tokens"
            else:
                usage = (
                    f"{g.up} {compact_token_count(tokens_in)} "
                    f"{g.down} {compact_token_count(tokens_out)}"
                )
            quality = usage_quality_label(usage_status, screen_reader=reader)
            if quality:
                usage += f", {quality}" if reader else f" {g.sep} {quality}"
        else:
            usage = usage_quality_label(usage_status, screen_reader=reader)

        cost = cost_label(cost_value, cost_status, screen_reader=reader)
        separator = ", " if reader else f"  {g.pipe}  "
        fields: list[tuple[str, str]] = []
        fields.append(
            (
                "turns",
                f"{turns} turn{'s' if turns != 1 else ''}"
                if reader
                else f"[dim]{turns} turn{'s' if turns != 1 else ''}[/dim]",
            )
        )
        fields.append(("cost", escape(cost) if reader else f"[dim]{escape(cost)}[/dim]"))
        usage_token = p.info if usage_status == "complete" else p.warning
        fields.append(("usage", escape(usage) if reader else f"[{usage_token}]{escape(usage)}[/]"))

        def render_fields() -> str:
            return separator.join(value for _, value in fields)

        for removable in ("turns", "cost"):
            if visible_len(render_fields()) <= cols:
                break
            fields = [field for field in fields if field[0] != removable]

        summary_line = render_fields()
        summary_lines = [summary_line]
        if reader and visible_len(summary_line) > cols and usage_status in {"complete", "partial"}:
            quality_suffix = (
                f", {usage_quality_label(usage_status, screen_reader=True)}"
                if usage_status == "partial"
                else ""
            )
            summary_lines = [
                f"Input: {tokens_in:,} tokens",
                f"Output: {tokens_out:,} tokens{quality_suffix}",
            ]
        elif visible_len(summary_line) > cols:
            summary_lines = [fit(summary_line, cols)]

        identity = identity_rows(sid, current_directory, cols)
        if not reader:
            identity = [f"[dim]{escape(line)}[/dim]" for line in identity]
        header_lines = [*identity, *summary_lines]
        for line in header_lines:
            self._console.print(line, markup=not reader, highlight=False)
        self._console.print(f"[dim]{g.rule * cols}[/dim]", markup=True, highlight=False)


# ── built-in renderers — registered at module load time, after class is complete ──


@register_renderer("turn_start")
def _render_turn_start(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    """No header for the default agent on an unchanged model (TUX-06 §2.5).

    A header — ``◆ reviewer · sonnet-4.6 · 14:02`` — marks a change of agent
    or model, or a return after more than 30 minutes.  It is printed after
    the user's mention chips, which belong to the user message above it.
    """
    from rich.text import Text

    self._group_count = 0
    agent = _text(ev.payload, "agent", "default") or "default"
    model = _text(ev.payload, "model")
    identity = (agent, model)
    previous = self._turn_identity
    self._turn_identity = identity
    changed = previous != identity if previous is not None else agent != "default"
    quiet_for = ev.timestamp - self._last_event_ts if self._last_event_ts is not None else 0.0
    if not changed and quiet_for <= _HEADER_GAP_S:
        return
    p, g = palette(), glyphs()
    label = "assistant" if agent == "default" else agent
    header = Text.assemble((f"{g.diamond} ", p.agent), (label, f"bold {p.agent}"))
    for part in (model, _hhmm(ev.timestamp)):
        if part:
            header.append(f" {g.sep} {part}", p.muted)
    self._pending_header = header


@register_renderer("user_message")
def _render_user_message(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    """The user's message as a ``user_bg`` band with a hanging indent (TUX-06 §2.5)."""
    from rich.table import Table
    from rich.text import Text

    p, g = palette(), glyphs()
    lines = _text(ev.payload, "text").splitlines() or [""]
    if len(lines) > 12:
        # A long paste is condensed; the full text stays in the event.
        lines = [*lines[:4], f"[+{len(lines) - 4} more lines]"]
    body = Text("\n".join(lines), style=p.text)
    if len(lines) > 1 and lines[-1].startswith("[+") and lines[-1].endswith("more lines]"):
        body.stylize(p.muted, len(body.plain) - len(lines[-1]))
    band = Table.grid(expand=True)
    band.add_column(width=2, no_wrap=True)
    band.add_column(ratio=1)
    band.add_row(Text(f"{g.prompt} ", style=f"bold {p.accent}"), body)
    self._open_block("user_message")
    self._console.print(band, style=p.on("user_bg"), highlight=False)
    self._gap_pending = True


@register_renderer("tool_complete")
def _render_tool_complete_ev(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    presentation = self._exploration_payload(ev.payload)
    if presentation is not None and not _ToolView.of(ev.payload).failed:
        self._add_exploration(ev.payload, presentation)
        return
    self._flush_exploration_group()
    self._group_count += 1
    shown = self._group_count - self._group_hidden
    if shown <= self._max_tool_calls or _ToolView.of(ev.payload).failed:
        self._print_pending_header()
        self._open_block("tool_complete")
        self._render_tool_complete(ev.payload)
        self._gap_pending = True
    else:
        self._group_hidden += 1
        self._state.conversation.live_tool_overflow.set(self._group_hidden)


@register_renderer("text")
def _render_text(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    text = _text(ev.payload, "text")
    if text.strip():
        from rich.markdown import Markdown

        self._console.print(Markdown(text), end="")
        # Keep each LLM response visually separated from the next event.
        # Markdown is printed with ``end=""`` so Rich controls its rendering;
        # this explicit empty print adds the trailing blank line consistently.
        self._console.print()


@register_renderer("thinking_step")
def _render_thinking_step(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    from rich.markup import escape as _e

    p, g = palette(), glyphs()
    step = _text(ev.payload, "step")
    done = ev.payload.get("done", False)
    icon = f"[{p.success}]{g.ok}[/]" if done else f"[{p.warning}]{g.right}[/]"
    self._console.print(
        f"  {icon} [dim]{_e(step)}[/dim]",
        markup=True,
        highlight=False,
    )


@register_renderer("file_modified")
def _render_file_modified(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    """An edit: ``● Edit path  +12 −3`` and its diff, 20 rows unless verbose."""
    from agenthicc.tui.diff_renderer import render_file_create, render_file_diff

    self._print_pending_header()
    self._open_block("file_modified")
    path = _text(ev.payload, "path")
    old_lines = ev.payload.get("old_lines")
    new_lines = ev.payload.get("new_lines")
    tool = _text(ev.payload, "tool", "write_file")
    old_line_list = _string_list(old_lines)
    new_line_list = _string_list(new_lines)
    lang = _lang_for_path(path)
    operation = tool_presentation(tool).operation
    max_rows = None if self._tool_detail() == "verbose" else _DIFF_ROWS

    if old_lines is not None and new_lines is not None:
        if old_line_list == []:
            self._console.print(
                render_file_create(path, new_line_list, language=lang), highlight=False
            )
        else:
            self._console.print(
                render_file_diff(
                    path,
                    old_line_list,
                    new_line_list,
                    operation=operation,
                    language=lang,
                    max_rows=max_rows,
                ),
                highlight=False,
            )
    else:
        p, g = palette(), glyphs()
        self._tool_row(g.bullet, p.muted, operation, path, "", None, path=True)
    self._gap_pending = True


@register_renderer("error")
def _render_error(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    self._flush_group_summary()
    from rich.markup import escape as _e

    msg = _text(ev.payload, "message")
    detail = _text(ev.payload, "detail")
    self._console.print(
        f"[red bold]ERROR[/red bold] {_e(msg)}",
        markup=True,
        highlight=False,
    )
    if detail:
        self._console.print(
            f"[dim]{_e(detail[:500])}[/dim]",
            markup=True,
            highlight=False,
        )


@register_renderer("turn_complete")
def _render_turn_complete(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    elapsed = _number(ev.payload, "elapsed_s")
    if elapsed >= 1.0:
        self._console.print(
            f"[dim]✾ Turn · {compact_duration(elapsed)}[/dim]",
            markup=True,
            highlight=False,
        )


@register_renderer("activity_complete")
def _render_activity_complete(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    """Render the total wall-clock duration for the completed user intent."""

    elapsed = _number(ev.payload, "elapsed_s")
    if elapsed >= 1.0:
        self._console.print(
            f"[dim]  Total {glyphs().sep} {compact_duration(elapsed)}[/dim]",
            markup=True,
            highlight=False,
        )
        self._gap_pending = True


@register_renderer("mention_chips")
def _render_mention_chips(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    """Mentions as chips under the user message (TUX-06 §2.5, §2.7).

    Resolved mentions share one quiet row (``@src/app.py  @docs/``); one that
    could not be resolved gets its own ``warning`` row with the reason.  It is
    the user's input that was wrong, so it never looks like a failed tool.
    """
    from rich.text import Text

    p, g = palette(), glyphs()
    chips = _mapping_list(ev.payload.get("chips", []))
    if self._last_block not in {"user_message", "mention_chips"}:
        self._open_block("mention_chips")
    self._last_block = "mention_chips"
    width = max(20, self._console.width)
    resolved = [_text(chip, "raw") for chip in chips if chip.get("ok", True) is not False]
    if resolved:
        row = Text("  ")
        for raw in resolved:
            row.append(raw if raw.startswith("@") else f"@{raw}", p.accent)
            row.append("  ")
        row.rstrip()
        row.truncate(width, overflow="ellipsis")
        self._console.print(row, highlight=False, no_wrap=True, overflow="ellipsis")
    for chip in chips:
        if chip.get("ok", True) is not False:
            continue
        raw = _text(chip, "raw")
        reason = _text(chip, "reason") or "could not be read"
        row = Text.assemble(
            (f"  {g.warn} ", f"bold {p.warning}"),
            (raw if raw.startswith("@") else f"@{raw}", p.warning),
            (f" {reason}", p.warning),
        )
        row.truncate(width, overflow="ellipsis")
        self._console.print(row, highlight=False, no_wrap=True, overflow="ellipsis")
    self._gap_pending = True


@register_renderer("replay_separator")
def _render_replay_separator(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    """``── replayed from 2 Nov 05:04 · 827 events ──`` … ``── now ──``."""
    from rich.text import Text

    p, g = palette(), glyphs()
    if ev.payload.get("edge") == "start":
        since = _number(ev.payload, "since", ev.timestamp)
        moment = time.localtime(since)
        label = f"replayed from {moment.tm_mday} {time.strftime('%b %H:%M', moment)}"
        count = _nonnegative_int(ev.payload, "events")
        if count:
            label += f" {g.sep} {_plural(count, 'event')}"
    else:
        label = "now"
    width = max(20, self._console.width)
    rule = f"{g.rule * 2} {label} "
    rule += g.rule * max(2, width - len(rule))
    self._console.print(Text(rule[:width], style=p.muted), highlight=False, no_wrap=True)
    self._gap_pending = False


# ── Generic system text (compactor, etc.) ─────────────────────────────────────


@register_renderer("system")
def _render_system(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    from rich.markup import escape as _e

    text = str(ev.payload.get("text", ""))
    if text:
        prefix = "  " if text.startswith("⎋ Compacted") else ""
        self._console.print(f"{prefix}[dim]{_e(text)}[/dim]", markup=True, highlight=False)
        self._console.print()


@register_renderer("announcement")
def _render_announcement(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    text = str(ev.payload.get("text", ""))
    if text:
        self._console.print(text, markup=False, highlight=False)


@register_renderer("tool_recovery")
def _render_tool_recovery(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    """Render one concise, redacted interrupted-exchange notice."""
    from rich.markup import escape as _e

    text = _text(
        ev.payload,
        "text",
        "Tool execution was interrupted; incomplete tool results were recorded so the session can continue safely.",
    )
    self._console.print(f"[dim]{_e(text)}[/dim]", markup=True, highlight=False)
    self._console.print()


@register_renderer("mcp_server_failure")
def _render_mcp_server_failure(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    """Render one generation-scoped, redacted MCP startup outcome."""
    from rich.markup import escape as _e

    text = _text(ev.payload, "text", "MCP server is unavailable; continuing without its tools.")
    style = "red" if bool(ev.payload.get("required", False)) else "yellow"
    self._console.print(f"[{style}]{_e(text)}[/{style}]", markup=True, highlight=False)
    self._console.print()


@register_renderer("network_retry")
def _render_network_retry(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    """Render a compact, readable provider retry notice.

    Transport exceptions often contain nested SDK dictionaries, provider
    names, status codes, and chained exception reprs.  That representation is
    useful in debug logs but makes the scroll buffer noisy and hard to scan.
    The retry producer supplies already-bounded, user-facing fields instead.
    """
    from rich.markup import escape as _e

    attempt = max(1, int(_number(ev.payload, "attempt", 1)))
    max_retries = max(attempt, int(_number(ev.payload, "max_retries", attempt)))
    delay = max(0.0, _number(ev.payload, "delay_s"))
    reason = _text(ev.payload, "reason", "Temporary network problem")
    detail = _text(ev.payload, "detail")
    status_code = ev.payload.get("status_code")
    status = (
        f"HTTP {status_code}"
        if isinstance(status_code, int) and not isinstance(status_code, bool)
        else ""
    )

    self._console.print(
        "[yellow]⟳[/yellow] [bold]Network issue[/bold] "
        f"[dim]retry {attempt}/{max_retries} in[/dim] "
        f"[cyan]{delay:.1f}s[/cyan]",
        markup=True,
        highlight=False,
    )
    metadata = " · ".join(part for part in (reason, status) if part)
    self._console.print(
        f"  [dim]{_e(metadata)}[/dim]" if metadata else "  [dim]Retrying…[/dim]",
        markup=True,
        highlight=False,
    )
    if detail and detail.casefold() != reason.casefold():
        self._console.print(f"  [dim]{_e(detail)}[/dim]", markup=True, highlight=False)
    self._console.print()


@register_renderer("provider_recovery_retry")
def _render_provider_recovery_retry(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    """Render a compact retry notice for a provider-rejected request."""
    from rich.markup import escape as _e

    attempt = max(1, int(_number(ev.payload, "attempt", 1)))
    max_retries = max(attempt, int(_number(ev.payload, "max_retries", attempt)))
    delay = max(0.0, _number(ev.payload, "delay_s"))
    detail = _text(ev.payload, "detail")
    status_code = ev.payload.get("status_code")
    status = (
        f"HTTP {status_code}"
        if isinstance(status_code, int) and not isinstance(status_code, bool)
        else "provider rejected request"
    )
    self._console.print(
        "[yellow]⚠[/yellow] [bold]Provider request rejected[/bold] "
        f"[dim]retry {attempt}/{max_retries} in[/dim] [cyan]{delay:.1f}s[/cyan]",
        markup=True,
        highlight=False,
    )
    self._console.print(f"  [dim]{_e(status)}[/dim]", markup=True, highlight=False)
    if detail:
        self._console.print(f"  [dim]{_e(detail)}[/dim]", markup=True, highlight=False)
    self._console.print()


@register_renderer("goal_list_mutated")
def _render_goal_list_mutated(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    """Render one polished notice after a durable goal append or insertion."""
    from rich.markup import escape as _e

    p = palette()
    heading, detail, continuation = _goal_mutation_lines(ev.payload)
    self._console.print(
        f"  [dim]⎿[/dim] [{p.accent}]{_e(heading)}[/]",
        markup=True,
        highlight=False,
    )
    self._console.print(
        f"    [dim]{_e(detail)}[/dim]",
        markup=True,
        highlight=False,
    )
    self._console.print(
        f"    [dim]{_e(continuation)}[/dim]",
        markup=True,
        highlight=False,
    )
    self._console.print()


def _phase_control_label(operation: str) -> str:
    """Return a bounded human label for a PRD-194 phase operation."""
    return {
        "postpone": "Postponed phase",
        "skip": "Skipped phase",
        "bring_forward": "Brought phase forward",
        "delete": "Deleted phase",
    }.get(operation, "Changed phase")


def _bounded_phase_id(value: object) -> str:
    """Keep opaque IDs readable without allowing a TUI line to grow unbounded."""
    text = str(value).strip()
    return text if len(text) <= 20 else f"{text[:17]}…"


@register_renderer("phase_control_requested")
def _render_phase_control_requested(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    """Render one compact safe-boundary request notice."""
    from rich.markup import escape as _e

    p = palette()
    operation = _text(ev.payload, "operation", "change")
    phase_id = _bounded_phase_id(ev.payload.get("phase_id", "unknown"))
    reason = _text(ev.payload, "reason")[:160]
    self._console.print(
        f"  [dim]⎿[/dim] [{p.warning}]{_e(_phase_control_label(operation))}[/] "
        f"[dim]{_e(phase_id)} at safe boundary[/dim]",
        markup=True,
        highlight=False,
    )
    if reason:
        self._console.print(f"    [dim]{_e(reason)}[/dim]", markup=True, highlight=False)
    self._console.print()


@register_renderer("phase_plan_mutated")
def _render_phase_plan_mutated(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    """Render one compact committed phase-plan notice."""
    from rich.markup import escape as _e

    p = palette()
    operation = _text(ev.payload, "operation", "change")
    phase_id = _bounded_phase_id(ev.payload.get("phase_id", "unknown"))
    disposition = _text(ev.payload, "disposition", "updated")
    reason = _text(ev.payload, "reason")[:160]
    self._console.print(
        f"  [dim]⎿[/dim] [{p.accent}]{_e(_phase_control_label(operation))}[/] "
        f"[dim]{_e(phase_id)} → {_e(disposition)}; checkpointed[/dim]",
        markup=True,
        highlight=False,
    )
    if reason:
        self._console.print(f"    [dim]{_e(reason)}[/dim]", markup=True, highlight=False)
    self._console.print()


@register_renderer("phase_control_rejected")
def _render_phase_control_rejected(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    """Render one bounded rejected phase-control notice."""
    from rich.markup import escape as _e

    p = palette()
    operation = _text(ev.payload, "operation", "change")
    phase_id = _bounded_phase_id(ev.payload.get("phase_id", "unknown"))
    error_code = _text(ev.payload, "error_code", "rejected")
    self._console.print(
        f"  [dim]⎿[/dim] [{p.danger}]Phase control rejected[/] "
        f"[dim]{_e(operation)} {_e(phase_id)} ({_e(error_code)})[/dim]",
        markup=True,
        highlight=False,
    )
    self._console.print()


# ── Subagent pool renderers (PRD-124 Phase 3) ─────────────────────────────────


@register_renderer("subagent_pool_started")
def _render_subagent_pool_started(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    from rich.markup import escape as _e

    from agenthicc.tui.rendering import fit, visible_len

    p = palette()
    cols = max(1, self._console.width)
    workers = _mapping_list(ev.payload.get("workers", []))
    total = max(_nonnegative_int(ev.payload, "total"), len(workers))
    title = f"◈ Subagents · {total} worker{'s' if total != 1 else ''} started"
    title = fit(_e(title), max(1, cols - 2))
    self._console.print(f"[bold]{title}[/bold]", markup=True, highlight=False)

    for worker in workers[:3]:
        raw_label = worker.get("label", "Worker")
        label = " ".join(str(raw_label).split()) or "Worker"
        task_value = worker.get("task", worker.get("type", ""))
        task = " ".join(str(task_value).split())
        prefix = f"  [dim]└[/dim] [{p.accent}]{_e(label)}[/]  "
        remaining = max(0, cols - visible_len(prefix))
        preview = fit(_e(task), remaining) if task and remaining else ""
        line = prefix + (f"[dim]{preview}[/dim]" if preview else "")
        self._console.print(fit(line, cols), markup=True, highlight=False)

    remaining_workers = total - min(3, len(workers))
    if remaining_workers > 0:
        more = fit(_e(f"  +{remaining_workers} more · Ctrl+O details"), cols)
        self._console.print(f"[dim]{more}[/dim]", markup=True, highlight=False)


@register_renderer("subagent_worker_done")
@register_renderer("subagent_worker_failed")
def _render_subagent_worker_done(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    from rich.markup import escape as _e

    p = palette()
    ok = bool(ev.payload.get("ok", True))
    label = _e(str(ev.payload.get("label", "")))
    done = ev.payload.get("done", 0)
    total = ev.payload.get("total", 0)
    ms = _number(ev.payload, "duration_ms")
    dur = f"  [dim]{ms / 1_000:.1f}s[/dim]"
    if ok:
        changed_paths = ev.payload.get("changed_paths", [])
        if isinstance(changed_paths, list) and changed_paths:
            detail = "  Modified " + ", ".join(_e(str(path)) for path in changed_paths[:3])
            if len(changed_paths) > 3:
                detail += f" (+{len(changed_paths) - 3} more)"
        else:
            summary = str(ev.payload.get("text", "")).strip().splitlines()[0:1]
            detail = f"  {_e(summary[0][:120])}" if summary else ""
        self._console.print(
            f"[{p.success}]✓[/] [{done}/{total}] [{p.accent}]{label}[/]{dur}{detail}",
            markup=True,
            highlight=False,
        )
    else:
        error = _e(str(ev.payload.get("error", "failed"))[:80])
        self._console.print(
            f"[{p.danger}]✗[/] [{done}/{total}] [{p.accent}]{label}[/]  [dim]{error}[/dim]",
            markup=True,
            highlight=False,
        )


@register_renderer("subagent_transcript_degraded")
def _render_subagent_transcript_degraded(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    failures = _nonnegative_int(ev.payload, "transcript_write_failures")
    self._console.print(
        f"  [{palette().warning}]![/] [dim]Subagent transcripts are partial: "
        f"{failures} write{'s' if failures != 1 else ''} failed[/dim]",
        markup=True,
        highlight=False,
    )


@register_renderer("subagent_pool_done")
def _render_subagent_pool_done(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    from rich.markup import escape as _e

    succeeded = _nonnegative_int(ev.payload, "succeeded")
    total = _nonnegative_int(ev.payload, "total")
    failed = _nonnegative_int(ev.payload, "failed")
    reason = ev.payload.get("budget_exceeded_reason", "")
    status = "complete" if succeeded == total and failed == 0 and not reason else "Failed"
    parts = [f"{succeeded}/{total} done"]
    if failed:
        parts.append(f"{failed} unsuccessful (failed or stopped)")
    suffix = " · ".join(parts)
    if isinstance(reason, str) and reason:
        suffix += f" · budget exceeded: {_e(reason.replace('_', ' '))}"
    self._console.print(
        f"[bold]◈ Subagents {status} · {suffix}[/bold]",
        markup=True,
        highlight=False,
    )
    self._console.print()
