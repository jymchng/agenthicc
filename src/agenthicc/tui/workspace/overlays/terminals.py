"""Paginated live list overlay for PRD-149 owned background terminals."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from rich.console import RenderableType
    from agenthicc.background.terminals import TerminalManager, TerminalRecord


class TerminalListOverlay(Overlay):
    """Inspect, tail, and stop terminals owned by the current session.

    The manager owns the records; this class owns only a bounded presentation
    projection and selection.  The selected terminal ID is authoritative so a
    manager refresh cannot make a stop key target a different record.
    """

    name = "terminals"

    def __init__(
        self,
        manager: "TerminalManager",
        on_close: Callable[[], None],
        *,
        selected_id: str = "",
    ) -> None:
        self._manager = manager
        self._on_close = on_close
        self._selected_id: str | None = selected_id or None
        self._index = 0
        self._viewport_width = 80
        self._viewport_height = 12
        self._details = False
        self._unsub: Callable[[], None] | None = None

    # Rich's SIMPLE table contributes a title, a blank line, a header, a
    # separator, and one line per record.  Group adds a separator between the
    # table/detail/footer renderables.  Keep these budgets explicit so the
    # footer is not delegated to Live's crop behavior.
    _NORMAL_OVERHEAD = 10  # table framing + detail group + footer
    _COMPACT_OVERHEAD = 5  # borderless table framing + footer
    _MAX_OUTPUT_CHARS = 240
    _MAX_OUTPUT_LINES = 1

    def on_mount(self) -> None:
        self._unsub = self._manager.changed.subscribe(self._on_change)
        self._select_requested()

    def on_unmount(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def _on_change(self) -> None:
        # OverlayHost redraws after key events; manager changes are also
        # projected through TUISession's status subscription.  Keeping this
        # method intentionally side-effect free makes refreshes safe during
        # process teardown.
        self._select_requested()

    def _records(self) -> list["TerminalRecord"]:
        return self._manager.list_records()

    def set_viewport(self, width: int, height: int) -> None:
        """Update the available overlay size without changing selection."""

        self._viewport_width = max(1, width)
        self._viewport_height = max(1, height)

    def _select_requested(self, records: list["TerminalRecord"] | None = None) -> None:
        records = self._records() if records is None else records
        requested_id = self._selected_id
        if requested_id:
            for index, record in enumerate(records):
                if record.terminal_id == requested_id:
                    self._index = index
                    return
        if records:
            self._index = min(self._index, len(records) - 1)
            self._selected_id = records[self._index].terminal_id
            if requested_id and requested_id != self._selected_id:
                # A details view must never silently switch to a replacement
                # terminal after its original record disappeared.
                self._details = False
        else:
            self._index = 0
            self._selected_id = None
            self._details = False

    def _selected(self, records: list["TerminalRecord"] | None = None) -> "TerminalRecord | None":
        records = self._records() if records is None else records
        self._select_requested(records)
        return records[self._index] if records else None

    def _page_size(self) -> int:
        overhead = self._NORMAL_OVERHEAD if self._viewport_height >= 11 else self._COMPACT_OVERHEAD
        return max(1, self._viewport_height - overhead)

    def _page_count(self, records: list["TerminalRecord"]) -> int:
        size = self._page_size()
        return max(1, (len(records) + size - 1) // size)

    def _page_bounds(self, records: list["TerminalRecord"]) -> tuple[int, int]:
        size = self._page_size()
        page = self._index // size if records else 0
        start = page * size
        return start, min(len(records), start + size)

    @staticmethod
    def _shorten(value: object, limit: int) -> str:
        compact = " ".join(str(value).split()) if value is not None else ""
        compact = compact or "—"
        if len(compact) <= limit:
            return compact
        return compact[: max(1, limit - 1)].rstrip() + "…"

    def _output_preview(self, record: "TerminalRecord") -> str:
        raw = record.stdout or record.stderr or "(no output)"
        lines = [" ".join(line.split()) for line in raw.splitlines() if line.strip()]
        preview = " … ".join(lines[-self._MAX_OUTPUT_LINES :]) or "(no output)"
        return self._shorten(preview, self._MAX_OUTPUT_CHARS)

    def _text_line(self, value: str, *, style: str = "") -> "RenderableType":
        from rich.text import Text  # noqa: PLC0415

        text = Text(value, style=style, no_wrap=True, overflow="crop")
        text.truncate(self._viewport_width, overflow="crop")
        return text

    @staticmethod
    def _cell(value: str, *, style: str = "") -> "RenderableType":
        """Render manager data literally so Rich markup is never evaluated."""

        from rich.text import Text  # noqa: PLC0415

        return Text(value, style=style, no_wrap=True, overflow="crop")

    def _footer(self, *, details: bool = False) -> "RenderableType":
        if details:
            hint = "Esc back   s stop   Ctrl+X stop"
        elif self._viewport_width < 48:
            hint = "↑↓ PgUp/PgDn   s   Esc close"
        else:
            hint = "↑↓/j/k select   PgUp/PgDn page   Home/End   Enter details   s stop   Ctrl+X stop   Esc close"
        return self._text_line(hint, style="dim")

    def render(self) -> "RenderableType":
        from rich.console import Group  # noqa: PLC0415
        from rich.table import Table  # noqa: PLC0415
        from rich import box  # noqa: PLC0415

        records = self._records()
        self._select_requested(records)
        selected = self._selected(records)

        if self._details and selected is not None:
            if self._viewport_height <= 2:
                return self._footer(details=True)
            if self._viewport_height < 6:
                return Group(
                    self._text_line(
                        f"Terminal Details · {self._shorten(selected.terminal_id, 32)}",
                        style="bold cyan",
                    ),
                    self._text_line(f"State: {self._shorten(selected.state.value, 24)}"),
                    self._footer(details=True),
                )
            return Group(
                self._text_line(
                    f"Terminal Details · {self._shorten(selected.terminal_id, 32)}",
                    style="bold cyan",
                ),
                self._text_line(f"State: {self._shorten(selected.state.value, 24)}"),
                self._text_line(f"Command: {self._shorten(selected.command, 120)}"),
                self._text_line(f"PID: {selected.pid or '—'}  Duration: {selected.elapsed_s:.1f}s"),
                self._text_line(f"Output: {self._output_preview(selected)}"),
                self._footer(details=True),
            )

        if self._viewport_height <= 3:
            return self._footer()

        start, end = self._page_bounds(records)
        page = (start // self._page_size()) + 1 if records else 1
        pages = self._page_count(records)
        if records:
            title = (
                f"Background Terminals · page {page}/{pages} · "
                f"showing {start + 1}–{end} of {len(records)}"
            )
        else:
            title = "Background Terminals · page 1/1 · showing 0 of 0"

        compact = self._viewport_height < 11
        table = Table(
            title=self._shorten(title, self._viewport_width),
            box=None if compact else box.SIMPLE,
            expand=True,
            pad_edge=False,
        )
        table.add_column("", width=2, no_wrap=True, overflow="crop")
        table.add_column("Handle", style="bold", no_wrap=True, overflow="crop")
        table.add_column("State", no_wrap=True, overflow="crop")
        table.add_column("Label", no_wrap=True, overflow="crop")
        table.add_column("Exit", no_wrap=True, overflow="crop")
        for index in range(start, end):
            record = records[index]
            table.add_row(
                self._cell("▶" if index == self._index else " "),
                self._cell(self._shorten(record.terminal_id, 28), style="bold"),
                self._cell(self._shorten(record.state.value, 16)),
                self._cell(self._shorten(record.label, 48)),
                self._cell(
                    "—" if record.returncode is None else self._shorten(record.returncode, 8)
                ),
            )
        if not records:
            table.add_row(
                self._cell(""),
                self._cell("—"),
                self._cell("—"),
                self._cell("No owned terminals"),
                self._cell("—"),
            )

        if compact:
            detail = (
                self._text_line(
                    f"Selected: {self._shorten(selected.terminal_id, 32)}"
                    if selected
                    else "No owned background terminals."
                )
                if self._viewport_height >= 6
                else None
            )
        else:
            detail = Group(
                self._text_line(
                    f"State: {selected.state.value}"
                    if selected
                    else "No owned background terminals."
                ),
                self._text_line(
                    f"Command: {self._shorten(selected.command, 120)}" if selected else ""
                ),
                self._text_line(
                    f"PID: {selected.pid or '—'}  Duration: {selected.elapsed_s:.1f}s"
                    if selected
                    else ""
                ),
                self._text_line(f"Output: {self._output_preview(selected)}" if selected else ""),
            )
        if detail is None:
            return Group(table, self._footer())
        return Group(table, detail, self._footer())

    def handle_key(self, key: Key, ch: str) -> bool:
        records = self._records()
        if key == Key.ESC:
            if self._details:
                self._details = False
                return True
            self._on_close()
            return True
        self._select_requested(records)
        if key == Key.UP or (key == Key.CHAR and ch.lower() == "k"):
            self._move(records, -1)
            return True
        if key == Key.DOWN or (key == Key.CHAR and ch.lower() == "j"):
            self._move(records, 1)
            return True
        if key == Key.PAGE_UP:
            self._move(records, -self._page_size())
            return True
        if key == Key.PAGE_DOWN:
            self._move(records, self._page_size())
            return True
        if key == Key.HOME:
            self._set_index(records, 0)
            return True
        if key == Key.END:
            self._set_index(records, max(0, len(records) - 1))
            return True
        if key == Key.ENTER:
            if self._selected(records) is not None:
                self._details = True
            return True
        selected = self._selected(records)
        if key == Key.CHAR and ch.lower() == "s":
            if selected:
                self._manager.request_stop(selected.terminal_id)
            return True
        if ch == "\x18":  # Ctrl+X, shared with the background-session manager
            if selected:
                self._manager.request_stop(selected.terminal_id)
            return True
        return True

    def _set_index(self, records: list["TerminalRecord"], index: int) -> None:
        if not records:
            self._index = 0
            self._selected_id = None
            return
        self._index = max(0, min(len(records) - 1, index))
        self._selected_id = records[self._index].terminal_id

    def _move(self, records: list["TerminalRecord"], delta: int) -> None:
        self._set_index(records, self._index + delta)
