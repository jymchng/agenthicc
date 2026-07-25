"""Live list overlay for PRD-149 owned background terminals."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from rich.console import RenderableType
    from agenthicc.background.terminals import TerminalManager, TerminalRecord


class TerminalListOverlay(Overlay):
    """Inspect, tail, and stop terminals owned by the current session."""

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
        self._selected_id = selected_id
        self._index = 0
        self._unsub: Callable[[], None] | None = None

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

    def _select_requested(self) -> None:
        records = self._records()
        if self._selected_id:
            for index, record in enumerate(records):
                if record.terminal_id == self._selected_id:
                    self._index = index
                    return
        if records:
            self._index = min(self._index, len(records) - 1)
        else:
            self._index = 0

    def _selected(self) -> "TerminalRecord | None":
        records = self._records()
        return records[self._index] if records and self._index < len(records) else None

    def render(self) -> "RenderableType":
        from rich.console import Group  # noqa: PLC0415
        from rich.table import Table  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415
        from rich import box  # noqa: PLC0415

        records = self._records()
        table = Table(title="Background Terminals", box=box.SIMPLE, expand=True)
        table.add_column("", width=2)
        table.add_column("Handle", style="bold")
        table.add_column("State")
        table.add_column("Label")
        table.add_column("Exit")
        for index, record in enumerate(records):
            marker = "▶" if index == self._index else " "
            table.add_row(
                marker,
                record.terminal_id,
                record.state.value,
                record.label,
                "—" if record.returncode is None else str(record.returncode),
            )
        selected = self._selected()
        detail_lines = [
            f"State: {selected.state.value}" if selected else "No owned background terminals.",
        ]
        if selected:
            detail_lines.extend(
                [
                    f"Command: {selected.command}",
                    f"PID: {selected.pid or '—'}  Duration: {selected.elapsed_s:.1f}s",
                    "Output:",
                    (selected.stdout or selected.stderr or "(no output)")[-2_000:],
                ]
            )
        detail = Text("\n".join(detail_lines))
        footer = Text("↑/↓ select  Enter details  s stop  Ctrl+X stop  Esc close", style="dim")
        return Group(table, detail, footer)

    def handle_key(self, key: Key, ch: str) -> bool:
        records = self._records()
        if key == Key.ESC:
            self._on_close()
            return True
        if key == Key.UP or (key == Key.CHAR and ch.lower() == "k"):
            self._index = max(0, self._index - 1)
            return True
        if key == Key.DOWN or (key == Key.CHAR and ch.lower() == "j"):
            self._index = min(max(0, len(records) - 1), self._index + 1)
            return True
        selected = self._selected()
        if key == Key.CHAR and ch.lower() == "s":
            if selected:
                self._manager.request_stop(selected.terminal_id)
            return True
        if ch == "\x18":  # Ctrl+X, shared with the background-session manager
            if selected:
                self._manager.request_stop(selected.terminal_id)
            return True
        return True
