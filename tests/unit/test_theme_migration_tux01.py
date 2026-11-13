"""TUX-01 goal 7: the hub, the subagent panel, the overlays and the mode badge
take every colour and glyph from the active theme when they render.

Each test switches themes with :func:`use_theme` between two renders of the
same object, so a colour captured at import or construction time would show
up as two identical renders.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from rich.console import Console
from rich.style import Style
from rich.text import Text

from agenthicc.subagents.pool import SubagentPoolState, WorkerState
from agenthicc.tui.runtime.mode_manager import build_mode_str, builtin_mode
from agenthicc.tui.theme import (
    ASCII_GLYPHS,
    BUILTIN_THEMES,
    current_theme,
    use_theme,
)
from agenthicc.tui.workspace.agents_hub.layout import layout
from agenthicc.tui.workspace.agents_hub.model import HubRow, LibraryEntry, RowKind
from agenthicc.tui.workspace.agents_hub.render import RowContext, row_line
from agenthicc.tui.workspace.overlays.registry_list import _ListRow, _RegistryListOverlay
from agenthicc.tui.workspace.subagent_panel import panel_lines, worker_line

pytestmark = pytest.mark.unit

NOW = 1_000_000.0
_CONSOLE = Console(width=120, color_system="truecolor", force_terminal=True)


@pytest.fixture(autouse=True)
def _restore_theme() -> Iterator[None]:
    before = current_theme()
    yield
    use_theme(before)


def _hex(style: Style, *, background: bool = False) -> str:
    color = style.bgcolor if background else style.color
    if color is None:
        return ""
    triplet = color.get_truecolor()
    return f"#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}"


def _style_of(text: Text, needle: str) -> Style:
    return text.get_style_at_offset(_CONSOLE, text.plain.index(needle))


def _definition_row(state: str = "invalid") -> HubRow:
    entry = LibraryEntry(name="reviewer", source="project", state=state, note="bad line 3")
    return HubRow(RowKind.DEFINITION, "definition:reviewer", entry=entry)


def _context() -> RowContext:
    return RowContext(now=NOW, queue_positions={}, sessions={})


def _worker(status: str) -> WorkerState:
    return WorkerState("explorer #1", "explorer", status, worker_id="w1", started_at=NOW - 5)


def test_a_hub_row_resolves_its_colours_when_it_renders() -> None:
    row = _definition_row()
    columns = layout(120)

    use_theme(BUILTIN_THEMES["dark"])
    dark = row_line(row, columns, _context(), selected=True)
    use_theme(BUILTIN_THEMES["light"])
    light = row_line(row, columns, _context(), selected=True)

    dark_p, light_p = BUILTIN_THEMES["dark"].palette, BUILTIN_THEMES["light"].palette
    # Invalid definitions are a warning; the source column is metadata.
    assert _hex(_style_of(dark, "reviewer")) == dark_p.warning
    assert _hex(_style_of(light, "reviewer")) == light_p.warning
    assert _hex(_style_of(dark, "project")) == dark_p.muted
    assert _hex(_style_of(light, "project")) == light_p.muted
    # The selected row: an accent bar on the selected-row background.
    assert dark.plain.startswith(BUILTIN_THEMES["dark"].glyphs.bar)
    assert _hex(_style_of(dark, dark.plain[0])) == dark_p.accent
    assert _hex(_style_of(dark, "project"), background=True) == dark_p.accent_bg
    assert _hex(_style_of(light, "project"), background=True) == light_p.accent_bg
    assert dark_p.warning != light_p.warning


def test_the_subagent_panel_resolves_its_colours_when_it_renders() -> None:
    state = SubagentPoolState("pool", 2, (_worker("running"), _worker("failed")), limit=2)

    def render() -> list[Text]:
        return [
            Text.from_markup(line)
            for line in panel_lines(state, 100, now=NOW, terminal_rows=40, selected="w1")
        ]

    use_theme(BUILTIN_THEMES["dark"])
    dark = render()
    use_theme(BUILTIN_THEMES["light"])
    light = render()

    for palette_name, lines in (("dark", dark), ("light", light)):
        p = BUILTIN_THEMES[palette_name].palette
        assert _hex(_style_of(lines[0], "Subagents")) == p.agent
        running, failed = lines[1], lines[2]
        glyph_set = BUILTIN_THEMES[palette_name].glyphs
        assert _hex(_style_of(running, glyph_set.bullet)) == p.info
        assert _hex(_style_of(failed, glyph_set.fail)) == p.danger
        # Worker w1 is selected: the accent bar on the selected-row background.
        assert _hex(_style_of(running, glyph_set.bar)) == p.accent
        assert _hex(_style_of(running, "Explorer"), background=True) == p.accent_bg
    assert [line.spans for line in dark] != [line.spans for line in light]


def test_worker_status_glyphs_follow_the_glyph_set() -> None:
    use_theme(BUILTIN_THEMES["dark"].with_glyphs(ASCII_GLYPHS))
    for status in ("pending", "running", "waiting", "done", "failed", "stopped"):
        line = Text.from_markup(worker_line(_worker(status), 80, now=NOW, selected=True)).plain
        assert line.isascii(), line


def test_a_hub_row_uses_only_ascii_with_the_ascii_glyph_set() -> None:
    use_theme(BUILTIN_THEMES["dark"].with_glyphs(ASCII_GLYPHS))
    for state in ("active", "shadowed", "invalid", "untrusted"):
        line = row_line(_definition_row(state), layout(120), _context(), selected=True)
        assert line.plain.isascii(), line.plain


def test_the_mode_badge_uses_the_mode_tokens_of_the_active_theme() -> None:
    for name, token in (("Safe", "mode_safe"), ("Plan", "mode_plan"), ("Yolo", "mode_yolo")):
        mode = builtin_mode(name)
        for theme_name in ("dark", "light"):
            use_theme(BUILTIN_THEMES[theme_name])
            badge = Text.from_markup(build_mode_str(mode))
            expected = getattr(BUILTIN_THEMES[theme_name].palette, token)
            assert _hex(_style_of(badge, name)) == expected


def test_a_registry_list_marks_the_selection_with_the_accent_bar_not_reverse() -> None:
    use_theme(BUILTIN_THEMES["dark"])
    rows = [_ListRow(f"cmd{index}", "builtin", "does a thing", ()) for index in range(3)]
    overlay = _RegistryListOverlay("Commands", rows, on_close=lambda: None)
    segments = list(_CONSOLE.render(overlay.render()))
    p = BUILTIN_THEMES["dark"].palette
    assert not any(segment.style is not None and segment.style.reverse for segment in segments)
    bar = next(segment for segment in segments if segment.text == "▌")
    assert bar.style is not None and _hex(bar.style) == p.accent
    selected = next(segment for segment in segments if "cmd0" in segment.text)
    assert selected.style is not None and _hex(selected.style, background=True) == p.accent_bg
