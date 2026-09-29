"""Regression coverage for PRD-202's paginated ``/ps`` overlay."""

from __future__ import annotations

from types import SimpleNamespace

from rich.console import Console

from agenthicc.background.terminals import TerminalRecord, TerminalState
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace.overlay import Overlay, OverlayHost
from agenthicc.tui.workspace.overlays.terminals import TerminalListOverlay


class _Manager:
    def __init__(self, records: list[TerminalRecord]) -> None:
        self.records = records
        self.stops: list[str] = []
        self.changed = SimpleNamespace(subscribe=lambda _callback: lambda: None)

    def list_records(self) -> list[TerminalRecord]:
        return list(self.records)

    def request_stop(self, terminal_id: str) -> None:
        self.stops.append(terminal_id)


class _ViewportOverlay(Overlay):
    name = "viewport-test"

    def __init__(self) -> None:
        self.viewport: tuple[int, int] | None = None

    def set_viewport(self, width: int, height: int) -> None:
        self.viewport = (width, height)

    def render(self):
        return "overlay"

    def handle_key(self, key: Key, ch: str) -> bool:  # noqa: ARG002
        return True


def _record(index: int, *, label: str | None = None) -> TerminalRecord:
    return TerminalRecord(
        terminal_id=f"term-{index:02d}",
        session_id="session",
        project_root="/tmp/project",
        cwd="/tmp/project",
        kind="exec",
        command=f"command-{index}",
        label=label or f"job-{index}",
        state=TerminalState.EXITED,
        created_at=float(index),
        returncode=0,
        stdout=f"output-{index}",
    )


def _render(overlay: TerminalListOverlay, *, width: int = 100) -> str:
    from io import StringIO

    output = StringIO()
    Console(file=output, width=width, force_terminal=False, markup=False).print(overlay.render())
    return output.getvalue()


def test_overlay_host_forwards_the_current_viewport_to_new_and_active_overlays() -> None:
    host = OverlayHost(AppState.create())
    host.set_viewport(72, 11)
    overlay = _ViewportOverlay()

    host.show(overlay)
    assert overlay.viewport == (72, 11)

    host.set_viewport(48, 7)
    assert overlay.viewport == (48, 7)


def test_large_terminal_list_is_paginated_and_keeps_controls_visible() -> None:
    manager = _Manager([_record(index) for index in range(15)])
    overlay = TerminalListOverlay(manager, lambda: None)
    overlay.set_viewport(100, 12)

    text = _render(overlay)

    assert "page 1/8" in text
    assert "showing 1–2 of 15" in text
    assert "term-00" in text
    assert "term-01" in text
    assert "term-02" not in text
    assert "job-0" in text and "  0" in text
    assert "Esc close" in text
    assert len(text.splitlines()) <= 12


def test_page_keys_and_vertical_navigation_reach_the_final_record() -> None:
    manager = _Manager([_record(index) for index in range(15)])
    overlay = TerminalListOverlay(manager, lambda: None)
    overlay.set_viewport(100, 18)  # Eight rows per page.
    overlay.on_mount()

    overlay.handle_key(Key.DOWN, "")
    overlay.handle_key(Key.DOWN, "")
    overlay.handle_key(Key.DOWN, "")
    assert "term-03" in _render(overlay)
    assert "page 1/2" in _render(overlay)

    overlay.handle_key(Key.PAGE_DOWN, "")
    assert "term-11" in _render(overlay)
    overlay.handle_key(Key.END, "")
    final = _render(overlay)
    assert "term-14" in final
    assert "page 2/2" in final


def test_requested_selection_survives_resize_and_refresh_by_terminal_id() -> None:
    records = [_record(index) for index in range(15)]
    manager = _Manager(records)
    overlay = TerminalListOverlay(manager, lambda: None, selected_id="term-08")
    overlay.set_viewport(100, 16)

    assert "term-08" in _render(overlay)
    assert "page 2/3" in _render(overlay)

    manager.records = [records[3], records[8], *records[0:3], *records[4:8], *records[9:]]
    overlay.set_viewport(60, 20)
    refreshed = _render(overlay)

    assert "term-08" in refreshed
    assert "page 1/2" in refreshed


def test_removed_selection_falls_back_without_stopping_a_different_old_id() -> None:
    records = [_record(index) for index in range(5)]
    manager = _Manager(records)
    overlay = TerminalListOverlay(manager, lambda: None, selected_id="term-04")
    overlay.set_viewport(100, 12)
    _render(overlay)

    manager.records = records[:4]
    overlay.handle_key(Key.CHAR, "s")

    assert manager.stops == ["term-03"]


def test_compact_narrow_layout_bounds_multiline_content_and_details_escape() -> None:
    manager = _Manager(
        [
            _record(
                0,
                label="long label\nwith another line [not markup] and more text than fits",
            )
        ]
    )
    manager.records[0].command = "command\nwith\nnewlines"
    manager.records[0].stdout = "secret-looking [bold] output\n" * 20
    overlay = TerminalListOverlay(manager, lambda: None)
    overlay.set_viewport(36, 6)

    compact = _render(overlay, width=36)
    assert "Esc close" in compact
    assert len(compact.splitlines()) <= 6
    assert "[bold]" not in compact

    overlay.handle_key(Key.ENTER, "")
    details = _render(overlay, width=36)
    assert "Terminal Details" in details
    assert len(details.splitlines()) <= 6
    overlay.handle_key(Key.ESC, "")
    assert "Background Terminals" in _render(overlay, width=36)
