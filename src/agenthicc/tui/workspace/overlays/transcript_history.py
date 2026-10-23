"""Bounded, on-demand browser for a session's persisted transcript."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Callable

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.runtime.session_log import (
    TranscriptPage,
    load_older_transcript_page,
    load_transcript_entry,
)
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from rich.console import RenderableType


class TranscriptHistoryOverlay(Overlay):
    """Read older transcript pages without inserting them into live scrollback.

    Page offsets are captured from the append-only log. A newly appended live
    event therefore does not shift cached pages or make historical events
    appear after newer output in the normal workspace.
    """

    name = "transcript_history"
    _PAGE_EVENTS = 64
    _PAGE_BYTES = 128 * 1024
    _EVENT_BYTES = 16 * 1024
    _DETAIL_LINES = 18
    _DETAIL_MAX_CHARS = 64 * 1024

    def __init__(
        self,
        session_id: str,
        initial_page: TranscriptPage,
        on_close: Callable[[], None],
    ) -> None:
        self._session_id = session_id
        self._pages = [initial_page]
        self._page_index = 0  # zero is the newest page
        self._on_close = on_close
        self._selected = 0
        self._detail: list[str] | None = None
        self._detail_scroll = 0
        self._viewport_height = 12
        self._notice = initial_page.notice

    def set_viewport(self, width: int, height: int) -> None:
        self._viewport_height = max(1, height)

    def render(self) -> "RenderableType":
        from rich.console import Group
        from rich.panel import Panel
        from rich.text import Text

        page = self._pages[self._page_index]
        if self._detail is not None:
            visible = self._detail[self._detail_scroll : self._detail_scroll + self._detail_rows()]
            body = "\n".join(visible) or "(empty event)"
            info = (
                f"Transcript detail · {self._detail_scroll + 1}–"
                f"{min(len(self._detail), self._detail_scroll + len(visible))}/{len(self._detail)}"
            )
            return Group(
                Panel(body, title=info, border_style="cyan"),
                Text("↑↓/PgUp/PgDn scroll   Esc back to events   q close history", style="dim"),
            )

        lines = [Text("Transcript History", style="bold cyan")]
        if page.entries:
            for index, entry in enumerate(page.entries):
                selected = index == self._selected
                event = entry.event
                timestamp = _timestamp(event.timestamp)
                preview = _event_preview(event.payload)
                line = Text("▶ " if selected else "  ", style="reverse" if selected else "")
                line.append(f"{timestamp}  {event.kind:<22} ", style="dim")
                line.append(preview)
                if selected:
                    line.stylize("reverse")
                lines.append(line)
        else:
            lines.append(Text("(No events on this page)", style="dim"))

        page_label = "newest" if self._page_index == 0 else f"{self._page_index + 1} pages back"
        has_older = page.older_cursor is not None
        lines.append(
            Text(
                f"Page {page_label} · {len(page.entries)} events · "
                f"{'older available' if has_older else 'start of transcript'}",
                style="dim",
            )
        )
        if self._notice:
            lines.append(Text(self._notice, style="yellow"))
        lines.extend(
            [
                Text(""),
                Text(
                    "↑↓ select   Enter inspect   PgUp/[ older   PgDn/] newer   Esc return live",
                    style="dim",
                ),
            ]
        )
        return Group(*lines)

    def handle_key(self, key: Key, ch: str) -> bool:
        if ch.lower() == "q" and key == Key.CHAR:
            if self._detail is not None:
                self._detail = None
                self._detail_scroll = 0
            else:
                self._on_close()
            return True
        if self._detail is not None:
            return self._handle_detail_key(key)

        page = self._pages[self._page_index]
        match key:
            case Key.ESC:
                self._on_close()
            case Key.UP:
                self._selected = max(0, self._selected - 1)
            case Key.DOWN:
                self._selected = min(max(0, len(page.entries) - 1), self._selected + 1)
            case Key.PAGE_UP:
                self._older()
            case Key.PAGE_DOWN:
                self._newer()
            case Key.ENTER:
                self._open_selected()
            case Key.CHAR if ch == "[":
                self._older()
            case Key.CHAR if ch == "]":
                self._newer()
        return True

    def _older(self) -> None:
        if self._page_index + 1 < len(self._pages):
            self._page_index += 1
            self._selected = 0
            self._notice = self._pages[self._page_index].notice
            return
        cursor = self._pages[-1].older_cursor
        if cursor is None:
            self._notice = "You are at the beginning of this transcript."
            return
        page = load_older_transcript_page(
            self._session_id,
            cursor,
            max_events=self._PAGE_EVENTS,
            max_bytes=self._PAGE_BYTES,
            max_event_bytes=self._EVENT_BYTES,
        )
        if page.older_cursor == cursor and not page.entries:
            self._notice = page.notice or "No older page could be read."
            return
        self._pages.append(page)
        self._page_index += 1
        self._selected = 0
        self._notice = page.notice

    def _newer(self) -> None:
        if self._page_index > 0:
            self._page_index -= 1
            self._selected = 0
            self._notice = self._pages[self._page_index].notice
        else:
            self._notice = "You are viewing the newest transcript page."

    def _open_selected(self) -> None:
        page = self._pages[self._page_index]
        if not page.entries:
            return
        self._selected = max(0, min(self._selected, len(page.entries) - 1))
        event = load_transcript_entry(page.entries[self._selected])
        if event is None:
            self._notice = "Unable to load the selected transcript event."
            return
        payload = _safe_detail_value(event.payload)
        document = {
            "event_id": event.event_id,
            "kind": event.kind,
            "timestamp": event.timestamp,
            "payload": payload,
        }
        rendered = json.dumps(document, ensure_ascii=False, indent=2, default=str)
        if len(rendered) > self._DETAIL_MAX_CHARS:
            rendered = (
                rendered[: self._DETAIL_MAX_CHARS]
                + "\n… detail truncated at the 64 KiB display limit …"
            )
        self._detail = rendered.splitlines()
        self._detail_scroll = 0

    def _handle_detail_key(self, key: Key) -> bool:
        rows = self._detail_rows()
        total = len(self._detail or [])
        match key:
            case Key.ESC | Key.LEFT:
                self._detail = None
                self._detail_scroll = 0
            case Key.UP:
                self._detail_scroll = max(0, self._detail_scroll - 1)
            case Key.DOWN:
                self._detail_scroll = min(max(0, total - rows), self._detail_scroll + 1)
            case Key.PAGE_UP:
                self._detail_scroll = max(0, self._detail_scroll - rows)
            case Key.PAGE_DOWN:
                self._detail_scroll = min(max(0, total - rows), self._detail_scroll + rows)
            case Key.HOME:
                self._detail_scroll = 0
            case Key.END:
                self._detail_scroll = max(0, total - rows)
        return True

    def _detail_rows(self) -> int:
        return max(3, min(self._DETAIL_LINES, self._viewport_height - 5))


def _timestamp(value: float) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(value)) if value > 0 else "—"
    except (OverflowError, OSError, ValueError):
        return "—"


def _event_preview(payload: dict[str, object], limit: int = 100) -> str:
    for key in ("text", "message", "summary", "tool", "name", "path", "label"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return _shorten(value, limit)
    safe_payload = _safe_detail_value(payload)
    try:
        return _shorten(json.dumps(safe_payload, ensure_ascii=False, default=str), limit)
    except (TypeError, ValueError):
        return "(event payload unavailable)"


def _shorten(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: max(1, limit - 1)].rstrip() + "…"


def _safe_detail_value(value: object) -> object:
    """Remove private/reasoning fields recursively before presenting history."""

    hidden_keys = {"analysis", "reasoning", "reasoning_content", "thinking"}
    if isinstance(value, dict):
        return {
            key: _safe_detail_value(item)
            for key, item in value.items()
            if isinstance(key, str) and not key.startswith("_") and key.lower() not in hidden_keys
        }
    if isinstance(value, list):
        return [_safe_detail_value(item) for item in value]
    return value
