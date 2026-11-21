"""A blank line precedes every heading in the agents hub but the first, so groups do not run together.

The blank line is a row of its own: the list scrolls and keeps the selected row in view by
counting rows, so a line drawn only by the renderer would push the selection off the screen
once several headings are showing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.background import BackgroundStore, SessionStatus
from agenthicc.config import UISettings
from agenthicc.tui.cbreak_reader import Key, MouseEvent
from agenthicc.tui.workspace.agents_hub import AgentsHub
from agenthicc.tui.workspace.agents_hub.model import HubRow, RowKind, note, spaced
from agenthicc.tui.workspace.agents_hub.model import header as heading
from tests.unit.test_agents_hub import _create, _open, _text

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path: Path) -> BackgroundStore:
    return BackgroundStore(tmp_path / "background")


def _row(key: str) -> HubRow:
    return HubRow(RowKind.SESSION, key, group="g")


# ── the model ────────────────────────────────────────────────────────────────


def test_every_heading_but_the_first_gets_one_blank_row_straight_before_it() -> None:
    rows = [heading("Needs you"), _row("a"), _row("b"), heading("Running"), _row("c")]
    assert [row.key for row in spaced(rows)] == [
        "header:Needs you",
        "a",
        "b",
        "spacer:Running",
        "header:Running",
        "c",
    ]


def test_the_first_heading_sits_right_under_the_tab_bar() -> None:
    assert [row.kind for row in spaced([heading("Needs you"), _row("a")])][0] is RowKind.HEADER


def test_an_empty_groups_hint_stays_with_its_heading() -> None:
    rows = [heading("Queued"), note("Queued", "Nothing waits"), heading("Running"), _row("a")]
    assert [row.kind for row in spaced(rows)] == [
        RowKind.HEADER,
        RowKind.NOTE,
        RowKind.SPACER,
        RowKind.HEADER,
        RowKind.SESSION,
    ]


def test_rows_without_headings_are_left_alone() -> None:
    rows = [_row("a"), _row("b")]
    assert spaced(rows) == rows
    assert spaced([]) == []


def test_the_blank_row_is_not_something_to_select() -> None:
    rows = spaced([heading("Needs you"), _row("a"), heading("Running")])
    assert [row.selectable for row in rows if row.kind is RowKind.SPACER] == [False]
    assert _row("a").selectable


# ── the drawn list ───────────────────────────────────────────────────────────


async def _hub_with_groups(store: BackgroundStore, tmp_path: Path) -> AgentsHub:
    _create(store, "approve", SessionStatus.WAITING_APPROVAL, 5)
    _create(store, "work-1", SessionStatus.RUNNING, 10)
    _create(store, "work-2", SessionStatus.RUNNING, 20)
    _create(store, "later", SessionStatus.QUEUED, 30)
    _create(store, "done", SessionStatus.COMPLETED, 40)
    return await _open(store, tmp_path)


def _headings(hub: AgentsHub) -> list[str]:
    return [row.text for row in hub.rows if row.kind is RowKind.HEADER]


async def test_the_line_before_each_heading_but_the_first_is_blank(
    store: BackgroundStore, tmp_path: Path
) -> None:
    hub = await _hub_with_groups(store, tmp_path)
    try:
        headings = _headings(hub)
        assert len(headings) >= 3
        lines = _text(hub, height=40).splitlines()
        at = {
            title: next(i for i, line in enumerate(lines) if line.strip() == title)
            for title in headings
        }
        # The first heading follows the tab bar; every later one is set off from the group above.
        assert lines[at[headings[0]] - 1].lstrip().startswith("Agents")
        for title in headings[1:]:
            assert lines[at[title] - 1].strip() == "", f"{title!r} runs into the group above it"
            assert lines[at[title] - 2].strip() != ""
        # And the first row of a group sits directly under its heading.
        for title in headings:
            assert lines[at[title] + 1].strip() != "", f"{title!r} has a gap under it"
    finally:
        await hub.stop()


async def test_the_needs_you_tab_lists_nothing_extra(
    store: BackgroundStore, tmp_path: Path
) -> None:
    """Only the list gets blank rows; the tab counts are of agents, not of lines."""
    hub = await _hub_with_groups(store, tmp_path)
    try:
        assert _text(hub, height=40).count("Needs you 1") == 1
    finally:
        await hub.stop()


async def test_moving_never_stops_on_the_blank_row(store: BackgroundStore, tmp_path: Path) -> None:
    hub = await _hub_with_groups(store, tmp_path)
    try:
        visited = [hub.selected_row()]
        for _ in range(len([row for row in hub.rows if row.selectable]) + 1):
            hub.handle_key(Key.DOWN, "")
            visited.append(hub.selected_row())
        assert all(row is not None and row.selectable for row in visited)
        assert [row.key for row in visited if row is not None][:5] == [
            "session:approve",
            "session:work-1",
            "session:work-2",
            "session:later",
            "session:done",
        ]
        for _ in range(8):
            hub.handle_key(Key.UP, "")
        assert hub.selected_row() is not None and hub.selected_row().key == "session:approve"  # type: ignore[union-attr]
    finally:
        await hub.stop()


async def test_a_click_on_the_blank_line_selects_nothing(
    store: BackgroundStore, tmp_path: Path
) -> None:
    hub = await _hub_with_groups(store, tmp_path)
    try:
        _text(hub, height=40)
        before = hub.selected_row()
        line = next(
            line
            for line, index in hub._geometry.rows.items()
            if hub.rows[index].kind is RowKind.SPACER
        )
        hub.handle_key(Key.MOUSE, MouseEvent(0, 5, line + 1, pressed=True).encode())
        assert hub.selected_row() == before
        assert hub.detail_session_id is None
    finally:
        await hub.stop()


async def test_the_selection_stays_on_screen_with_every_heading_showing(
    store: BackgroundStore, tmp_path: Path
) -> None:
    """The reason the blank line is a row: the window counts rows to keep the selection in view."""
    hub = await _hub_with_groups(store, tmp_path)
    try:
        for _ in range(10):
            hub.handle_key(Key.DOWN, "")
        selected = hub.selected_row()
        assert selected is not None and selected.key == "session:done"
        for height in (10, 12, 14):
            text = _text(hub, height=height)
            assert "done" in text, f"the selected row scrolled out of a {height}-line view"
    finally:
        await hub.stop()


async def test_a_screen_reader_is_not_read_blank_lines(
    store: BackgroundStore, tmp_path: Path
) -> None:
    hub = await _hub_with_groups(store, tmp_path)
    try:
        hub._state.ui.set(UISettings(screen_reader=True))
        body = _text(hub, height=40).splitlines()
        at = next(i for i, line in enumerate(body) if line.strip().startswith("Running:"))
        assert body[at - 1].strip() != ""
    finally:
        await hub.stop()


async def test_the_plain_listing_stays_one_row_per_line(
    store: BackgroundStore, tmp_path: Path
) -> None:
    """``agenthicc agents`` output is read line by line; it does not gain blank lines."""
    hub = await _hub_with_groups(store, tmp_path)
    try:
        lines = hub.listing()
        assert "" not in lines
        assert any(line.endswith(":") for line in lines)  # headings are still there
    finally:
        await hub.stop()
